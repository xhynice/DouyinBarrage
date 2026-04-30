"""采集器主类：WebSocket 连接管理、消息分发、心跳、看门狗、等待开播。

DouyinBarrage 是整个采集流程的协调中心，组合以下模块：
    base.parser      消息解析与分发表
    base.utils       配置加载、Cookie、工具函数
    base.output      日志、数据记录
    service.network  HTTP 请求、WebSocket 构建、房间 API
    service.signer   签名生成

线程模型：
    主线程    WebSocket 连接循环（含重连逻辑）
    daemon    心跳线程（每 N 秒发送 hb）
    daemon    看门狗线程（检测静默断连）
    daemon    统计线程（定时打印吞吐量）
    daemon    监控线程（等待开播模式下的 HTTP 轮询）
"""

import gzip
import json
import logging
import os
import random
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from socket import SOL_SOCKET, SO_RCVBUF, setdefaulttimeout, getdefaulttimeout

import requests
from requests.adapters import HTTPAdapter
import websocket

logging.getLogger('urllib3').setLevel(logging.CRITICAL)

from base.messages import PushFrame, Response, parse_proto
from base.parser import HANDLERS
from base.utils import (
    load_config, load_cookies,
    USER_AGENTS, LOW_VALUE_TYPES, INTERACTIVE_TYPES, METHOD_TO_CONFIG,
    generate_user_unique_id, extract_ua_version,
    rotate_ua,
)
from base.output import setup_logger, DataRecorder, ThroughputCounter, BARRAGE, RoomLogFilter, display_width, is_ci_environment
from service.network import (
    fetch_ttwid, enter_room_api, download_image,
    build_http_headers,
    build_websocket_url, build_ws_cookie,
)
from service.signer import generate_signature

logger = logging.getLogger(__name__)


class DouyinBarrage:
    """抖音直播间弹幕数据采集器。

    通过 WebSocket 长连接实时获取 13 种消息类型，输出 CSV/JSONL。
    支持登录态、自动重连、等待开播、弱网容错。

    Attributes:
        _DEFAULT_CONFIG: 统一默认配置，与 config.yaml 做浅合并。
    """

    # 统一默认配置
    _DEFAULT_CONFIG = {
        'log_level': 'INFO',
        'output': {
            'chat': True, 'lucky_bag': True, 'gift': True, 'like': True,
            'member': True, 'social': True, 'rank': True, 'stats': True,
            'fansclub': True, 'emoji': True, 'room': True, 'roomstats': True,
            'control': True, 'file_format': 'none', 'file_dir': 'data',
        },
        'network': {
            'http_timeout': 15, 'ws_connect_timeout': 30, 'silence_timeout': 60,
            'heartbeat_interval': 10, 'rcvbuf_kb': 256, 'proxy': None,
        },
        'max_reconnects': 0,
        'reconnect_base_delay': 2,
        'reconnect_max_delay': 120,
        'stats_interval': 60,
        'cookie_file': 'cookie.txt',
        'live_stop': False,
        'live_check_interval': 30,
    }

    def __init__(self, live_id, config_file='config.yaml', log_level=None, on_room_info=None, multi_room=False):
        """初始化采集器。

        Args:
            live_id: 直播间 ID（web_rid）。
            config_file: 配置文件路径（默认 config.yaml）。
            log_level: 日志级别覆盖（None 时使用配置文件中的值）。
            on_room_info: 可选回调，首次获取房间信息后调用。
                          签名: on_room_info(room_id: str, anchor_name: str)
            multi_room: 多房间模式，控制台仅显示状态面板。
        """
        self._on_room_info = on_room_info
        # ── 配置 ──
        self.config = load_config(config_file, self._DEFAULT_CONFIG)

        # ── 日志 ──
        effective_level = (log_level or self.config.get('log_level', 'INFO')).upper()
        self._logger, self._queue_handler = setup_logger(
            log_dir='logs',
            log_level=effective_level,
            multi_room=multi_room,
        )

        self._enable_outputs = self.config.get('output', {})

        # ── UA（一次选定，全局一致）──
        self._ua = random.choice(USER_AGENTS)
        self._ua_version = extract_ua_version(self._ua)
        self._user_unique_id = generate_user_unique_id()

        # ── 网络超时参数 ──
        net_cfg = self.config.get('network', {})
        self._http_timeout = net_cfg.get('http_timeout', 15)
        self._ws_connect_timeout = net_cfg.get('ws_connect_timeout', 30)
        self._silence_timeout = net_cfg.get('silence_timeout', 60)
        self._heartbeat_interval = net_cfg.get('heartbeat_interval', 10)
        self._proxy = net_cfg.get('proxy', None)
        self._rcvbuf = net_cfg.get('rcvbuf_kb', 256) * 1024

        # ── HTTP Session ──
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=2)
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)
        self.session.headers.update(build_http_headers(self._ua, self._ua_version))
        if self._proxy:
            self.session.proxies.update(self._proxy)
            logger.info(f"[启动] 使用代理: {self._proxy}")

        # ── 登录 Cookie ──
        self._cookie_file = self.config.get('cookie_file', 'cookie.txt')
        self._login_cookies = load_cookies(self._cookie_file)
        if self._login_cookies:
            for name, value in self._login_cookies.items():
                self.session.cookies.set(name, value, domain='.douyin.com')
            has_session = bool(self._login_cookies.get('sessionid') or
                               self._login_cookies.get('sessionid_ss'))
            if has_session:
                logger.info(f"[启动] 已加载 Cookie（{len(self._login_cookies)} 项），包含 sessionid，待连接后验证登录态")
            else:
                logger.info(f"[启动] 已加载 Cookie（{len(self._login_cookies)} 项），未包含 sessionid，将以游客身份采集")
        else:
            logger.info("[启动] 未加载 cookie.txt，以游客身份采集（礼物等信息可能受限）")

        # ── 直播间 ──
        self.live_id = live_id

        # ── 连接状态 ──
        self.ws = None
        self._connected_event = threading.Event()
        self._stop_event = threading.Event()
        self._conn_stop = threading.Event()

        # ── 线程引用 ──
        self._heartbeat_thread = None
        self._watchdog_thread = None
        self._stats_thread = None

        # ── 健康检测 ──
        self._last_msg_time = 0.0
        self._last_msg_time_lock = threading.Lock()

        # ── 业务消息健康检测（检测"有数据但无业务消息"的假死状态）──
        self._last_business_msg_time = 0.0
        self._last_business_msg_time_lock = threading.Lock()
        self._ws_connected_at = 0.0  # 连接建立时间，看门狗用于计算业务沉默

        # ── 吞吐量 ──
        self._counter = ThroughputCounter()

        # ── 数据记录器（首次连接后初始化）──
        self._recorder = None

        # ── 统计定时打印 ──
        self._stats_interval = self.config.get('stats_interval', 60)

        # ── 连接重试 ──
        self._reconnect_count = 0
        self._ttwid_refresh_needed = False

        # ── ttwid 缓存 ──
        self._ttwid = None
        self._login_info = {'is_login': False, 'nickname': '', 'uid': ''}

        # ── 房间信息 ──
        self._room_id = None
        self._room_info = None

        # ── 等待开播 ──
        self._live_lock = threading.Lock()
        self._waiting_live = False
        self._live_event = threading.Event()
        self._monitor_stop = None
        self._monitor_done = None

        # ── 预计算 enable_outputs 缓存（_wsOnOpen 中更新）──
        self._eo_cached = dict(self._enable_outputs)

        # ── 面板刷新节流 ──
        self._panel_last = 0.0

    @property
    def anchor_name(self):
        return self._room_info.get('anchor_name', '') if self._room_info else ''

    @property
    def display_name(self):
        """显示用名称：优先主播名，降级为 live_id。"""
        return self.anchor_name or self.live_id

    # ── 懒加载属性 ────────────────────────────────

    @property
    def ttwid(self):
        """获取 ttwid，首次访问触发 HTTP 请求并缓存。

        Side Effects:
            首次访问时请求 live.douyin.com 获取 ttwid Cookie，
            同时验证登录态（is_login / nickname），输出身份验证日志。
            解析 sid_guard Cookie 提取有效期并格式化显示。

        Returns:
            ttwid 字符串。
        """
        if self._ttwid:
            return self._ttwid
        self._ttwid, self._login_info = fetch_ttwid(
            self.session, self.live_id,
            self._login_cookies, self._http_timeout,
        )
        # 登录态判定
        has_cookie = bool(self._login_cookies.get('sessionid') or
                          self._login_cookies.get('sessionid_ss'))
        # 提取 cookie 有效期
        expire_date = ''
        sid_guard = self._login_cookies.get('sid_guard', '')
        if sid_guard:
            decoded = urllib.parse.unquote(sid_guard)
            parts = decoded.split('|')
            if len(parts) >= 4:
                # 格式: "Thu, 11-Jun-2026 10:31:57 GMT" → 取日期部分
                date_str = parts[3].replace('+', ' ').strip()
                # 格式化为年月日: "11-Jun-2026" → "2026-06-11"
                m_date = re.search(r'(\d+)-(\w+)-(\d+)', date_str)
                if m_date:
                    day, mon_str, year = m_date.group(1), m_date.group(2), m_date.group(3)
                    months = {'Jan':'01','Feb':'02','Mar':'03','Apr':'04','May':'05','Jun':'06',
                              'Jul':'07','Aug':'08','Sep':'09','Oct':'10','Nov':'11','Dec':'12'}
                    mon = months.get(mon_str[:3], '00')
                    expire_date = f'{year}-{mon}-{day}'

        if self._login_info['is_login']:
            nick = self._login_info['nickname']
            logger.info(f"[房间] 已登录「{nick}」")
            if expire_date:
                logger.info(f"[房间] Cookie 有效期至 {expire_date}")
        elif has_cookie:
            logger.warning("[房间] Cookie 中存在 sessionid，但服务端返回未登录状态，"
                           "cookie 可能已过期，请重新从浏览器导出")
            logger.info("[房间] 以游客模式采集（礼物等信息可能受限）")
        else:
            logger.info("[房间] 无登录凭证，以游客模式采集（礼物等信息可能受限）")
        return self._ttwid

    @property
    def room_id(self):
        """获取直播间真实 room_id，首次访问触发 HTTP 请求。

        Side Effects:
            首次访问时调用 enter_room_api 获取房间信息，
            输出房间状态和主播名称日志。

        Returns:
            room_id 字符串。
        """
        if self._room_id:
            return self._room_id
        self._room_info = enter_room_api(
            self.ttwid, self._ua, self._ua_version,
            self.live_id, self._http_timeout, session=self.session,
        )
        self._room_id = self._room_info['room_id']
        status = self._room_info['status']
        status_text = {2: '直播中', 4: '未开播'}.get(status, f'未知({status})')
        logger.info(f'[房间] room_id={self._room_id}, 状态={status_text}, 主播={self.anchor_name}')
        return self._room_id

    # ── 启动 / 停止 ──────────────────────────────

    def start(self):
        """启动采集，进入 WebSocket 连接主循环。"""
        logger.info(f"[启动] live_id: {self.live_id}")
        logger.info(f"[启动] UA: {self._ua}")
        logger.info(f"[启动] user_unique_id: {self._user_unique_id}")
        logger.info(f"[启动] 网络配置: http_timeout={self._http_timeout}s, "
                     f"ws_connect_timeout={self._ws_connect_timeout}s, "
                     f"silence_timeout={self._silence_timeout}s, "
                     f"heartbeat_interval={self._heartbeat_interval}s, "
                     f"rcvbuf={self._rcvbuf // 1024}KB"
                     f"{', proxy=on' if self._proxy else ''}")
        self._connectWebSocket()

    def stop(self):
        """停止采集，关闭 WebSocket，停止所有线程，输出最终统计。

        幂等操作，重复调用无副作用。
        """
        if self._stop_event.is_set():
            return
        logger.info("[控制] 停止采集")
        self._stop_event.set()
        self._live_event.set()  # 解除主循环在 wait_live 中的阻塞
        self._connected_event.clear()
        self._stop_monitor_loop()
        self._queue_handler.clear_room_status(self.live_id)
        if self.ws:
            try:
                self.ws.keep_running = False
                # 强制关闭底层 socket，避免 close() 阻塞在发送 close frame 上
                if self.ws.sock:
                    self.ws.sock.close()
                self.ws.close()
            except Exception as e:
                logger.debug(f"[连接] WebSocket 关闭异常: {e}")
        for t in (self._heartbeat_thread, self._watchdog_thread, self._stats_thread):
            if t and t.is_alive():
                t.join(timeout=3)
        logger.info(f"[统计] 最终: {self._counter.report()}")
        if self._recorder:
            self._recorder.close()
        # 多实例共享 QueueHandler，不在此处关闭（由进程退出统一清理）
        # 单实例模式下 stop() 后进程通常也退出，无需显式关闭

    # ── 状态消息 ──────────────────────────────────

    def _state_json(self, event, live, message, **extra):
        """生成结构化状态 JSON 字符串（供数据管道消费）。

        Args:
            event: 事件类型标识。
            live: 是否直播中。
            message: 人类可读的消息文本。
            **extra: 额外字段（如 retry_interval_seconds）。

        Returns:
            JSON 字符串。
        """
        return json.dumps({
            'type': 'system',
            'event': event,
            'live': live,
            'room_id': self.live_id,
            'anchor_name': self.anchor_name,
            'message': message,
            **extra,
        }, ensure_ascii=False)

    def _log_status(self, event, live, message, **extra):
        """输出人类可读的状态日志 + 结构化 JSON。

        Args:
            event: 事件类型标识。
            live: 是否直播中。
            message: 人类可读的消息文本。
            **extra: 额外字段（如 retry_interval_seconds）。

        Returns:
            JSON 字符串（同 _state_json）。
        """
        prefix = f"[直播状态] {self.anchor_name} " if self.anchor_name else "[直播状态] "
        logger.info(f"{prefix}{message}"
                    + (f" ({', '.join(f'{k}={v}' for k, v in extra.items())})" if extra else ''))
        return self._state_json(event, live, message, **extra)

    # ── 等待开播 ──────────────────────────────────

    def _enter_wait_mode(self):
        """直播结束，进入等待开播模式。

        Side Effects:
            重置计数器和数据记录器，关闭当前 WebSocket，
            启动 HTTP 轮询监控线程。
        """
        with self._live_lock:
            if self._waiting_live:
                return
            self._waiting_live = True
        poll_interval = self.config.get('live_check_interval', 30)
        label = self.display_name
        logger.info(f'[控制] {label} 监测中（间隔 {poll_interval}s）')
        if self._queue_handler.multi_room:
            self._queue_handler.set_room_status(
                self.live_id, 'waiting',
                anchor=self.display_name,
                interval=poll_interval,
            )
        else:
            sys.stderr.write('\n')
        self._counter = ThroughputCounter()
        self._reset_recorder()
        if self.ws:
            try:
                self.ws.keep_running = False
                # 强制关闭底层 socket，避免 close() 阻塞
                if self.ws.sock:
                    self.ws.sock.close()
                self.ws.close()
            except Exception as e:
                logger.debug(f"[连接] 等待模式关闭异常：{e}")

        self._start_monitor_loop()

    def _is_waiting_live(self):
        """检查是否处于等待开播模式。

        Returns:
            True 表示正在等待开播。
        """
        with self._live_lock:
            return self._waiting_live

    def _reset_recorder(self):
        """关闭并重建数据记录器（幂等操作）。"""
        if self._recorder:
            try:
                self._recorder.close()
            except Exception as e:
                logger.debug(f"[数据] 关闭旧 recorder 异常: {e}")
        self._recorder = DataRecorder(self.live_id, self.config)

    def _start_monitor_loop(self):
        """启动等待开播的监控循环（HTTP 轮询 + 状态通知）。

        多房间模式：更新状态面板，由面板统一显示。
        单房间模式：使用 \\r 动态光标动画。
        """
        if self._monitor_stop is not None:
            return
        stop_event = threading.Event()
        done_event = threading.Event()
        self._monitor_stop = stop_event
        self._monitor_done = done_event

        poll_interval = self.config.get('live_check_interval', 30)
        room_label = f'{self.anchor_name} ' if self.anchor_name else f'{self.live_id} '
        is_multi = self._queue_handler.multi_room

        def loop():
            try:
                # 等待初始化完成（如 on_room_info 回调），避免 \r 动画覆盖日志
                stop_event.wait(0.3)
                if stop_event.is_set() or self._stop_event.is_set():
                    return
                start_time = time.time()
                while not stop_event.is_set() and not self._stop_event.is_set():
                    try:
                        info = enter_room_api(
                            self.ttwid, self._ua, self._ua_version,
                            self.live_id, self._http_timeout, session=self.session,
                        )
                        if info['status'] == 2:
                            self._room_id = info['room_id']
                            self._room_info = info
                            self._on_live_started(source='api')
                            return
                    except Exception as e:
                        logger.warning(f'[监控] API 检查失败: {e}')
                        if any(kw in str(e).lower() for kw in ('sign', '403', 'unauthorized', 'cookie')):
                            logger.warning(f'[监控] 检测到认证异常，强制刷新 ttwid')
                            self._ttwid = None

                    if is_multi:
                        # 多房间：更新状态面板
                        self._queue_handler.set_room_status(
                            self.live_id, 'waiting',
                            anchor=self.display_name,
                            interval=poll_interval,
                        )
                        # 分段等待
                        for _ in range(int(poll_interval / 0.5)):
                            if stop_event.is_set() or self._stop_event.is_set():
                                break
                            time.sleep(0.5)
                    else:
                        for _ in range(int(poll_interval / 0.5)):
                            if stop_event.is_set() or self._stop_event.is_set():
                                break
                            elapsed = time.time() - start_time
                            remaining = max(0, int(poll_interval - elapsed))
                            cursor = '|' if int(elapsed * 2) % 2 == 0 else ' '
                            text = f'[等待开播] {room_label}轮询中{cursor} {remaining}s'
                            old_len = self._queue_handler._polling_len
                            pad = max(old_len - display_width(text), 0)
                            if is_ci_environment():
                                print(text)
                            else:
                                sys.stderr.write('\r' + text + ' ' * pad)
                                sys.stderr.flush()
                            self._queue_handler._polling_len = display_width(text)
                            time.sleep(0.5)
                    start_time = time.time()
            finally:
                if is_multi:
                    self._queue_handler.clear_room_status(self.live_id)
                else:
                    if not is_ci_environment():
                        sys.stderr.write('\r' + ' ' * self._queue_handler._polling_len + '\r')
                    self._queue_handler._polling_len = 0
                done_event.set()
                if self._monitor_stop is stop_event:
                    self._monitor_stop = None
                    self._monitor_done = None

        t = threading.Thread(target=loop, daemon=True, name=f'monitor-{self.live_id}')
        t.start()

    def _stop_monitor_loop(self):
        """停止监控循环，最多等待 3 秒。"""
        stop = self._monitor_stop
        done = self._monitor_done
        if stop is not None:
            stop.set()
        if done is not None:
            done.wait(timeout=3)

    def _on_live_started(self, source):
        """检测到开播，清理等待状态并通知主循环。

        Args:
            source: 检测来源标识（'api' / 'ws' / 'reconnect'）。
        """
        with self._live_lock:
            if not self._waiting_live:
                return
            self._waiting_live = False
        self._stop_monitor_loop()
        self._reset_recorder()
        self._counter = ThroughputCounter()
        self._reconnect_count = 0
        self._live_event.set()
        self._queue_handler.set_room_status(
            self.live_id, 'collecting',
            anchor=self.display_name,
            msg_count=0,
            elapsed=0,
        )
        label = self.display_name
        logger.info(f'[房间] {label} 已开播')
        logger.info(f"[房间] 检测到开播 (来源:{source})，重新连接...")

    # ── WebSocket 连接循环 ────────────────────────

    def _connectWebSocket(self):
        """WebSocket 连接主循环，包含重连逻辑。

        每次重连前：
        1. 重新获取 room_id（主播重开播可能换 ID）
        2. 检查直播状态，未开播时进入等待模式
        3. 刷新 ttwid（签名失败时）
        4. 切换 UA（降低风控）
        5. 指数退避延迟（base × 2^n，封顶 max_delay + 随机抖动）
        """
        max_reconnects = self.config.get('max_reconnects', 0)
        base_delay = self.config.get('reconnect_base_delay', 2)
        max_delay = self.config.get('reconnect_max_delay', 120)
        self._reconnect_count = 0

        while not self._stop_event.is_set():
            try:
                logger.info(f"[连接] 第 {self._reconnect_count + 1} 次连接")

                # ── 状态感知（每次重新获取 room_id，主播重开播可能换 ID）──
                self._room_id = None
                info = enter_room_api(
                    self.ttwid, self._ua, self._ua_version,
                    self.live_id, self._http_timeout, session=self.session,
                )
                self._room_id = info['room_id']
                self._room_info = info

                # 立即更新日志前缀映射（不等 set_room_status）
                anchor = info.get('anchor_name', '')
                if anchor:
                    RoomLogFilter.update_anchor(self.live_id, anchor)

                # 首次连接后触发回调（如自动补全配置中的主播名）
                if self._on_room_info and self._reconnect_count == 0:
                    try:
                        self._on_room_info(self.live_id, info.get('anchor_name', ''))
                    except Exception:
                        pass
                status = info['status']

                if status != 2:
                    status_text = {4: '未开播'}.get(status, f'未知({status})')
                    poll_interval = self.config.get('live_check_interval', 30)
                    # 刷新 ttwid（如果之前标记了需要刷新）
                    if self._ttwid_refresh_needed:
                        self._ttwid_refresh_needed = False
                        self._ttwid = None
                        try:
                            _ = self.ttwid
                            logger.info("[房间] ttwid 刷新成功")
                        except RuntimeError as e:
                            logger.error(f"[房间] ttwid 刷新失败: {e}，无法继续连接，请检查网络")
                            break
                    # 进入等待模式（不输出日志，由单行动态显示替代）
                    if not self._is_waiting_live():
                        self._enter_wait_mode()
                    while not self._stop_event.is_set():
                        if self._live_event.wait(timeout=1.0):
                            break
                    if self._stop_event.is_set():
                        break
                    self._live_event.clear()
                    self._reconnect_count = 0
                    continue
                else:
                    if self._is_waiting_live():
                        self._on_live_started(source='reconnect')
                        # 等待开播后首次连接：给服务端时间初始化消息路由
                        # 同时刷新 ttwid 和 user_unique_id，避免使用等待期间被污染的旧参数
                        logger.info("[连接] 检测到开播，等待 5 秒后建立 WebSocket（让服务端路由就绪）")
                        time.sleep(5)
                        self._ttwid = None
                        try:
                            _ = self.ttwid
                            logger.info("[连接] ttwid 已刷新")
                        except RuntimeError as e:
                            logger.warning(f"[连接] ttwid 刷新失败: {e}，使用现有值继续")
                    label = self.display_name
                    logger.info(f'[房间] {label} 直播中')
                    if not self._is_waiting_live():
                        self._queue_handler.set_room_status(
                            self.live_id, 'collecting',
                            anchor=self.display_name,
                            msg_count=0,
                            elapsed=0,
                        )

                # ttwid 签名校验失败时自动刷新
                if self._ttwid_refresh_needed:
                    self._ttwid_refresh_needed = False
                    self._ttwid = None
                    try:
                        _ = self.ttwid
                        logger.info("[房间] ttwid 刷新成功")
                    except RuntimeError as e:
                        logger.error(f"[房间] ttwid 刷新失败: {e}，无法继续连接，请检查网络")
                        break

                # 每次 WebSocket 连接前重新生成 user_unique_id，避免被之前 HTTP 轮询的行为污染
                old_uid = self._user_unique_id
                self._user_unique_id = generate_user_unique_id()
                logger.info(f"[连接] user_unique_id 已刷新: {old_uid} → {self._user_unique_id}")

                # 构建 WebSocket URL 并签名
                wss = build_websocket_url(self._room_id, self._user_unique_id, self._ua_version)
                signature = generate_signature(self._room_id, self._user_unique_id)
                if not signature:
                    logger.error("[签名] X-Bogus 签名生成失败，Node.js 未安装或 sign.js 异常，停止采集")
                    break
                wss += f"&signature={signature}"
                logger.debug(f"[签名] 生成: signature='{signature}', 长度={len(signature)}, "
                             f"user_unique_id={self._user_unique_id}, room_id={self._room_id}")

                headers = {
                    "cookie": build_ws_cookie(self.ttwid, self._login_cookies),
                    "user-agent": self._ua,
                }
                logger.debug(f"[连接] WS Cookie 前 80 字符: {headers['cookie'][:80]}...")

                self.ws = websocket.WebSocketApp(
                    wss,
                    header=headers,
                    on_open=self._wsOnOpen,
                    on_message=self._wsOnMessage,
                    on_error=self._wsOnError,
                    on_close=self._wsOnClose,
                )

                # 临时设置全局 socket 超时，确保 TCP connect 不会无限阻塞
                # run_forever 内部通过 getdefaulttimeout() 获取此值
                old_timeout = getdefaulttimeout()
                setdefaulttimeout(self._ws_connect_timeout)
                try:
                    self.ws.run_forever(
                    sockopt=((SOL_SOCKET, SO_RCVBUF, self._rcvbuf),),
                    ping_interval=30,
                    ping_timeout=10,
                    origin='https://live.douyin.com',
                )
                finally:
                    setdefaulttimeout(old_timeout)

            except RuntimeError as e:
                logger.error(f"[连接] WebSocket 不可恢复错误，停止采集: {e}")
                break
            except ValueError as e:
                err_str = str(e)
                if '4001038' in err_str or 'API 响应非 JSON' in err_str:
                    logger.error(f"[房间] 直播间无效（live_id={self.live_id}），停止采集: {e}")
                    break
                logger.error(f"[网络] API 异常: {e}")
            except Exception as e:
                logger.error(f"[连接] WebSocket 异常: {e}")

            self._connected_event.clear()

            if self._stop_event.is_set():
                break

            self._reconnect_count += 1
            if max_reconnects > 0 and self._reconnect_count >= max_reconnects:
                logger.error(f"[重连] 达到最大次数 ({max_reconnects})，停止")
                break

            # 重连前切换 UA，降低风控风险
            old_ua = self._ua
            self._ua, self._ua_version = rotate_ua(self._ua)
            if self._ua != old_ua:
                logger.debug(f"[重连] 刷新 UA: {old_ua[:50]}... → {self._ua[:50]}...")
                self.session.headers.update(build_http_headers(self._ua, self._ua_version))

            delay = min(base_delay * (2 ** min(self._reconnect_count - 1, 6)), max_delay)
            delay += random.uniform(0, 2)
            logger.warning(f"[重连] 断开，{delay:.1f}s 后重连 ({self._reconnect_count}"
                           f"{'/' + str(max_reconnects) if max_reconnects > 0 else ''})")
            self._stop_event.wait(timeout=delay)

        logger.info("[控制] 采集主循环退出")
        if self._queue_handler.multi_room:
            self._queue_handler.clear_room_status(self.live_id)

    # ── 心跳 / 看门狗 / 统计 ─────────────────────

    def _heartbeat_loop(self):
        """心跳线程，每 heartbeat_interval 秒发送二进制心跳包。"""
        conn_stop = self._conn_stop   # 缓存到局部变量，防止 _wsOnOpen 替换后旧线程不退出
        interval = max(self._heartbeat_interval, 3)
        while not conn_stop.is_set() and not self._stop_event.is_set():
            try:
                if self._connected_event.is_set():
                    self.ws.send(
                        PushFrame(payload_type="hb")._pb.SerializeToString(),
                        websocket.ABNF.OPCODE_BINARY,
                    )
            except Exception:
                break
            conn_stop.wait(timeout=interval + random.uniform(0, 2))

    def _watchdog_loop(self):
        """看门狗线程，检测静默断连。

        检查间隔为 silence_timeout / 3（最少 3s，最多 10s）。
        超过 silence_timeout 秒无消息时强制关闭 WebSocket 触发重连。
        同时检测连接建立阶段超时（_connected_event 长时间未置位）。
        """
        conn_stop = self._conn_stop
        check_interval = max(min(self._silence_timeout // 3, 10), 3)
        logger.debug(f"[看门狗] 线程启动，检查间隔={check_interval}s，超时阈值={self._silence_timeout}s")
        watchdog_start = time.time()
        try:
            while not conn_stop.is_set() and not self._stop_event.is_set():
                conn_stop.wait(timeout=check_interval)
                if not self._connected_event.is_set():
                    # 连接建立阶段也检测超时
                    elapsed = time.time() - watchdog_start
                    if elapsed > self._silence_timeout:
                        logger.warning(f"[看门狗] 连接建立超时 ({elapsed:.0f}s)，强制重连")
                        try:
                            if self.ws and self.ws.sock:
                                self.ws.sock.close()
                        except Exception:
                            pass
                        break
                    logger.debug("[看门狗] 连接未建立，跳过检查")
                    continue
                if self._last_msg_time <= 0:
                    logger.debug("[看门狗] 最后消息时间未初始化，跳过检查")
                    continue
                with self._last_msg_time_lock:
                    silence = time.time() - self._last_msg_time
                logger.debug(f"[看门狗] 静默时间: {silence:.0f}s")
                if silence > self._silence_timeout:
                    logger.warning(f"[看门狗] {silence:.0f}s 无数据 (阈值={self._silence_timeout}s)，触发重连")
                    try:
                        self.ws.keep_running = False
                        # 强制关闭底层 socket，避免 close() 阻塞在发送 close frame 上
                        if self.ws.sock:
                            self.ws.sock.close()
                        self.ws.close()
                    except Exception:
                        pass
                    break

                # 业务消息检测：有数据但无业务消息（低价值消息不断刷新 _last_msg_time 但无弹幕/礼物等）
                # _last_business_msg_time == 0 表示连接后从未收到真正的业务消息，
                # 此时用连接建立时间作为基准计算沉默时长
                if self._last_business_msg_time > 0:
                    with self._last_business_msg_time_lock:
                        business_silence = time.time() - self._last_business_msg_time
                else:
                    business_silence = time.time() - getattr(self, '_ws_connected_at', time.time())
                if business_silence > self._silence_timeout:
                    logger.info(f"[看门狗] {business_silence:.0f}s 无业务消息 (仅有低价值消息)，触发重连")
                    try:
                        self.ws.keep_running = False
                        if self.ws.sock:
                            self.ws.sock.close()
                        self.ws.close()
                    except Exception:
                        pass
                    break
        except Exception as e:
            logger.error(f"[看门狗] 线程异常: {e}")

    def _stats_loop(self):
        """统计线程，每 stats_interval 秒打印吞吐量报告。"""
        conn_stop = self._conn_stop
        while not conn_stop.is_set() and not self._stop_event.is_set():
            conn_stop.wait(timeout=self._stats_interval)
            if self._connected_event.is_set() and not self._is_waiting_live():
                logger.info(f"[统计] {self._counter.report()}")

    # ── WebSocket 回调 ────────────────────────────

    def _save_room_info(self):
        """保存主播信息和下载图片（meta.json 不存在时执行）。"""
        if not self._room_info:
            return

        file_dir = self.config.get('output', {}).get('file_dir', 'data')
        room_dir = os.path.join(file_dir, self.live_id)
        meta_file = os.path.join(room_dir, 'meta.json')

        if os.path.exists(meta_file):
            return

        try:
            os.makedirs(room_dir, exist_ok=True)

            meta = {
                'live_id': self.live_id,
                **self._room_info,
                'saved_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }

            with open(meta_file, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            logger.info(f"[数据] 主播信息已保存: {meta_file}")

            if download_image(self.session, self._room_info['anchor_avatar'],
                              os.path.join(room_dir, 'avatar.jpg')):
                logger.info(f"[数据] 主播头像已下载")

            if download_image(self.session, self._room_info['room_cover'],
                              os.path.join(room_dir, 'cover.jpg')):
                logger.info(f"[数据] 直播间封面已下载")
        except Exception as e:
            logger.warning(f"[数据] 保存主播信息失败: {e}")

    def _wsOnOpen(self, ws):
        """WebSocket 连接成功回调。

        Side Effects:
            启动心跳、看门狗、统计三个 daemon 线程。
        """
        logger.info("[连接] WebSocket 已建立")
        self._connected_event.set()
        with self._last_msg_time_lock:
            self._last_msg_time = time.time()
        # _last_business_msg_time 保持为 0，等第一条真正的业务消息到达时才更新
        # 这样看门狗能正确检测"连接成功但无业务消息"的假活状态
        self._ws_connected_at = time.time()  # 连接建立时间，看门狗用于计算业务沉默

        # 预计算 enable_outputs（每连接一次，避免每条消息拷贝）
        self._eo_cached = dict(self._enable_outputs)
        self._eo_cached['live_stop'] = self.config.get('live_stop', False)

        # 停止旧连接的线程，重建连接级停止信号
        # 关键：先 set 旧 Event（通知旧线程退出），等旧线程退出后，再替换为新 Event
        old_conn_stop = self._conn_stop
        old_conn_stop.set()
        for t in (self._heartbeat_thread, self._watchdog_thread, self._stats_thread):
            if t and t.is_alive():
                t.join(timeout=2)
                if t.is_alive():
                    logger.warning(f"[连接] 旧线程 {t.name} 未在 2 秒内退出")
        self._conn_stop = threading.Event()

        # 连接成功，重置重连计数器
        self._reconnect_count = 0

        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True, name='heartbeat')
        self._heartbeat_thread.start()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True, name='watchdog')
        self._watchdog_thread.start()
        self._stats_thread = threading.Thread(target=self._stats_loop, daemon=True, name='stats')
        self._stats_thread.start()

        # 提前初始化 recorder，避免首批消息（如统计、排行榜）因 recorder 未就绪而丢失
        # 注意：recorder 采用延迟初始化策略 —— __init__ 中不创建，直到首次连接成功才 open()
        # 因为 open() 需要 room_id（来自 enter_room_api），而首次调用可能因网络延迟失败
        if self._recorder is None:
            self._recorder = DataRecorder(self.live_id, self.config)
        self._recorder.open(self.room_id)

        # 首次连接时保存主播信息和下载图片
        self._save_room_info()

    def _wsOnMessage(self, ws, message):
        """WebSocket 消息回调，处理流程：PushFrame → gzip → Response → 分发。

        流程：
        1. 解析 PushFrame（protobuf 序列化帧）
        2. gzip 解压 payload
        3. 解析 Response（含消息列表）
        4. 发送 ACK（如 need_ack 为 True）
        5. 按 msg.method 分发到对应 handler
        6. 处理控制指令（stop / wait_live）

        Args:
            ws: WebSocketApp 实例。
            message: 原始二进制消息。
        """
        with self._last_msg_time_lock:
            self._last_msg_time = time.time()

        try:
            package = parse_proto(PushFrame, message)
        except Exception as e:
            logger.debug(f"[连接] PushFrame 解析失败: {e}")
            return

        # 心跳帧无 payload，跳过解压
        if package.payload_type == 'hb':
            return

        try:
            decompressed = gzip.decompress(package.payload)
        except gzip.BadGzipFile as e:
            logger.warning(f"[连接] gzip 损坏，丢弃本帧（可能是丢包/乱序）: {e}")
            return
        except Exception as e:
            logger.error(f"[连接] gzip 解压异常（非格式错误，需排查）: {e}")
            return

        try:
            response = parse_proto(Response, decompressed)
        except Exception as e:
            logger.warning(f"[数据] Response 解析失败: {e}")
            return

        # ACK
        if response.need_ack:
            try:
                ack = PushFrame(
                    log_id=package.log_id,
                    payload_type='ack',
                    payload=response.internal_ext.encode('utf-8'),
                )._pb.SerializeToString()
                ws.send(ack, websocket.ABNF.OPCODE_BINARY)
            except Exception as e:
                logger.error(f"[连接] ACK 发送失败: {e}")

        # 消息分发
        for msg in response.messages_list:
            handler = HANDLERS.get(msg.method)
            if handler:
                try:
                    results = handler(msg.payload, enable_outputs=self._eo_cached or {})
                    config_key = METHOD_TO_CONFIG.get(msg.method)
                    is_enabled = self._eo_cached.get(config_key, True) if config_key else True
                    short_name = msg.method.replace('Webcast', '').replace('Message', '').lower()
                    self._counter.inc(short_name, enabled=is_enabled)

                    # 追踪业务消息时间（仅交互类消息重置计时器）
                    # 排除 RoomRankMessage/RoomStatsMessage 等系统级消息——未开播也会推送，
                    # 会错误重置业务计时器导致看门狗无法检测假活状态
                    if msg.method in INTERACTIVE_TYPES:
                        with self._last_business_msg_time_lock:
                            prev = self._last_business_msg_time
                            self._last_business_msg_time = time.time()
                            if prev == 0:
                                delay = time.time() - getattr(self, '_ws_connected_at', 0)
                                logger.info(f"[连接] 首条业务消息到达: {msg.method} (连接后 {delay:.1f}s)")

                    # 每 3 秒刷新一次面板（节流，避免每条消息都更新）
                    now = time.monotonic()
                    if now - self._panel_last >= 3.0:
                        self._panel_last = now
                        elapsed = now - self._counter._start
                        self._queue_handler.set_room_status(
                            self.live_id, 'collecting',
                            anchor=self.display_name,
                            msg_count=self._counter._count,
                            elapsed=elapsed,
                        )

                    for r in results:
                        # 处理控制指令
                        if 'action' in r:
                            if r['action'] == 'stop':
                                logger.warning("[控制] 直播间已结束，停止采集")
                                self.stop()
                                return
                            elif r['action'] == 'wait_live':
                                self._enter_wait_mode()
                            continue

                        # 日志 + 记录
                        msg_text = r.get('msg', '')
                        if msg_text:
                            logger.log(BARRAGE, msg_text)

                        rec_type = r.get('type', '')
                        rec_data = r.get('data')
                        if rec_type and rec_type != '_log_only' and rec_data and self._recorder:
                            self._recorder.record(rec_type, rec_data)

                except Exception as e:
                    logger.error(f"[数据] 处理 {msg.method} 失败: {e}")

                # 等待开播检测：收到交互消息 = 开播
                if self._is_waiting_live() and msg.method in INTERACTIVE_TYPES:
                    self._on_live_started(source='ws')
            else:
                if msg.method in LOW_VALUE_TYPES:
                    logger.debug(f"[数据] 低价值消息（跳过）: {msg.method}")
                else:
                    self._counter.inc('unknown')
                    logger.debug(f"[数据] 未注册消息类型: {msg.method}")

    def _wsOnError(self, ws, error):
        """WebSocket 错误回调。

        处理两类特殊错误：
        - sign check / signature 失败 → 标记需要刷新 ttwid
        - DEVICE_BLOCKED → 提取握手信息并停止采集

        Args:
            ws: WebSocketApp 实例。
            error: 异常对象。
        """
        error_str = str(error)
        # 优雅关闭时产生的噪音日志，过滤掉
        if self._stop_event.is_set() and (error_str == '0' or not error_str or error_str == 'None'):
            return
        logger.error(f"[连接] WebSocket 错误: {error_str}")
        self._connected_event.clear()
        if ('sign check' in error_str or 'signature' in error_str) and 'DEVICE_BLOCKED' not in error_str:
            logger.warning("[签名] ttwid 签名校验失败，将在重连前尝试刷新 ttwid")
            self._ttwid_refresh_needed = True
        elif 'DEVICE_BLOCKED' in error_str:
            # 用正则提取握手响应关键信息，兼容不同引号格式
            def _extract(key):
                m = re.search(rf"['\"]?{re.escape(key)}['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]", error_str)
                return m.group(1) if m else '(未知)'

            handshake_status = _extract('handshake-status')
            handshake_msg = _extract('handshake-msg')
            trace_id = _extract('x-tt-trace-id')

            logger.error(
                f"[签名] DEVICE_BLOCKED，握手被拒，签名或端点不可用，停止采集\n"
                f"  handshake-status={handshake_status}, msg={handshake_msg}, trace-id={trace_id}\n"
                f"  请检查 sign.js 是否过期或尝试其他端点"
            )
            self._stop_event.set()

    def _wsOnClose(self, ws, code, msg):
        """WebSocket 关闭回调。

        Args:
            ws: WebSocketApp 实例。
            code: 关闭状态码。
            msg: 关闭消息。
        """
        logger.info(f"[连接] WebSocket 已关闭 (code={code})")
        self._connected_event.clear()
