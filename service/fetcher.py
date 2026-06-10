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
import threading
import time

# 抑制 websocket-client 库的 "Websocket connected" 原始输出
logging.getLogger('websocket').setLevel(logging.WARNING)
import urllib.parse
from datetime import datetime
from socket import SOL_SOCKET, SO_RCVBUF

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
    rotate_ua, sanitize_dir_name, get_anchor_dir,
    DEFAULT_CONFIG,
)
from base.output import setup_logger, DataRecorder, ThroughputCounter, RoomLogFilter
from service.network import (
    fetch_ttwid, enter_room_api, download_image,
    build_http_headers,
    build_websocket_url, build_ws_cookie,
    resolve_live_id,
    fetch_webcast_cursor,
    RoomNotFoundError,
)
from service.signer import generate_signature
from service.recorder import DouyinRecorder, check_ffmpeg
from base.stream import select_stream_url

logger = logging.getLogger(__name__)

# setdefaulttimeout 是全局操作，所有房间共用同一超时值，在模块加载时设置一次即可
# run_forever 内部每次重连都会读取 getdefaulttimeout()，因此不能设为 None
from socket import setdefaulttimeout as _setdefaulttimeout
_setdefaulttimeout(30)  # WS_CONNECT_TIMEOUT 的默认值


class DouyinBarrage:
    """抖音直播间弹幕数据采集器。

    通过 WebSocket 长连接实时获取 13 种消息类型，输出 CSV/SQLite。
    支持登录态、自动重连、等待开播、弱网容错。

    Attributes:
        _DEFAULT_CONFIG: 统一默认配置，与 config.yaml 做浅合并。
    """

    # ── 硬编码常量 ──
    HTTP_TIMEOUT = 15              # HTTP 超时（秒），超时后自动 ×1.5，封顶 60s，最多重试 3 次
    WS_CONNECT_TIMEOUT = 30        # WebSocket 底层 socket 超时（秒）
    SILENCE_TIMEOUT = 60           # 看门狗静默阈值（秒）
    HEARTBEAT_INTERVAL = 5         # 心跳间隔（秒）
    RCVBUF_KB = 512                # 接收缓冲区（KB）
    MAX_RECONNECTS = 5             # 最大重连次数（0 = 无限）
    RECONNECT_BASE_DELAY = 8       # 重连基础延迟（秒），指数退避：8s → 16s → 32s → ...
    RECONNECT_MAX_DELAY = 120      # 最大重连延迟（秒），退避封顶
    STATS_INTERVAL = 60            # 吞吐统计打印间隔（秒）

    COOKIE_FILE = 'cookie.txt'     # Cookie 文件路径（硬编码）

    # 统一默认配置（定义在 base.utils，此处引用保持兼容）
    _DEFAULT_CONFIG = DEFAULT_CONFIG

    def __init__(self, live_id, config_file='config.yaml', log_level=None, on_room_info=None):
        """初始化采集器。

        Args:
            live_id: 直播间 ID（web_rid）。
            config_file: 配置文件路径（默认 config.yaml）。
            log_level: 日志级别覆盖（None 时使用配置文件中的值）。
            on_room_info: 可选回调，首次获取房间信息后调用。
                          签名: on_room_info(room_id: str, anchor_name: str)
        """
        self._on_room_info = on_room_info
        # ── 配置 ──
        self.config = load_config(config_file, self._DEFAULT_CONFIG)

        # ── 日志 ──
        effective_level = (log_level or self.config.get('log_level', 'INFO')).upper()
        self._logger, self._queue_handler = setup_logger(
            log_dir='logs',
            log_level=effective_level,
        )

        self._enable_outputs = self.config.get('output', {})
        self._barrage_cfg = self.config.get('barrage', {})

        # ── UA（一次选定，全局一致）──
        self._ua = random.choice(USER_AGENTS)
        self._ua_version = extract_ua_version(self._ua)
        self._user_unique_id = generate_user_unique_id()
        self._uid_ever_used = False

        # 网络超时参数直接使用类常量

        # ── HTTP Session ──
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=2)
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)
        self.session.headers.update(build_http_headers(self._ua, self._ua_version))

        # ── 登录 Cookie（硬编码路径）──
        self._login_cookies = load_cookies(self.COOKIE_FILE)
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
        try:
            self.live_id = resolve_live_id(live_id, self.session, http_timeout=self.HTTP_TIMEOUT)
            if self.live_id != live_id:
                logger.info(f"[解析] 输入「{live_id}」→ web_rid: {self.live_id}")
        except ValueError as e:
            logger.error(f"[解析] 直播间地址解析失败: {e}")
            self.live_id = live_id

        # ── 连接状态 ──
        self.ws = None
        self._ws_lock = threading.Lock()
        self._connected_event = threading.Event()
        self._stop_event = threading.Event()
        self._stop_reason = ''  # 停止原因: ''=正常, 'room_not_found'=房间不存在
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
        self._data_recorder = None


        # ── 连接重试 ──
        self._reconnect_count = 0
        self._ttwid_refresh_needed = False

        # ── ttwid 缓存 ──
        self._ttwid = None
        self._login_info = {'is_login': False, 'nickname': '', 'uid': ''}

        # ── 房间信息 ──
        self._room_id = None
        self._room_info = None
        self._room_info_lock = threading.Lock()
        # room_info 过期标记: ffmpeg 崩后置 True,下次 _start_recording 重新查 API
        self._room_info_stale = False

        # ── 等待开播 ──
        self._live_lock = threading.Lock()
        self._waiting_live = False
        self._live_event = threading.Event()
        self._monitor_stop = None
        self._monitor_done = None

        # ── 预计算 enable_outputs 缓存（_wsOnOpen 中更新）──
        self._eo_cached = dict(self._enable_outputs)

        # ── 录制配置 ──
        self._record_cfg = self.config.get('record', {})
        self._video_recorder = None
        self._ws_url = ''
        self._stream_url = ''
        # 录制重启背压：上次尝试时间戳，防止 ffmpeg 反复崩时热循环
        self._last_recording_attempt = 0.0
        # 录制相关操作的锁（wsOnOpen/看门狗/recorder 回调三个来源并发）
        self._recording_lock = threading.Lock()

        # ── 面板刷新节流 ──
        self._panel_last = 0.0

    @property
    def anchor_name(self):
        with self._room_info_lock:
            return self._room_info.get('anchor_name', '') if self._room_info else ''

    @property
    def display_name(self):
        """显示用名称：优先主播名，降级为 live_id。"""
        return self.anchor_name or self.live_id

    def get_status_dict(self):
        """返回实例状态摘要（供外部 API 使用，避免直接访问私有属性）。

        Returns:
            dict: 包含 room_info, ws_url, stream_url, is_recording,
                  rec_elapsed, record_cfg 等字段。
        """
        with self._room_info_lock:
            info = dict(self._room_info) if self._room_info else {}
        is_recording = bool(
            self._video_recorder and self._video_recorder.is_recording
        )
        rec_elapsed = self._video_recorder.elapsed if is_recording else 0
        return {
            'room_title': info.get('room_title', ''),
            'room_id': info.get('room_id', ''),
            'sec_uid': info.get('sec_uid', ''),
            'ws_url': self._ws_url,
            'stream_url': self._stream_url,
            'is_recording': is_recording,
            'rec_elapsed': rec_elapsed,
            'record_cfg': dict(self._record_cfg),
            'record_dir': self._video_recorder.session_dir if is_recording else '',
        }

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
            self._login_cookies, self.HTTP_TIMEOUT,
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
            if nick and len(nick) > 2:
                hidden_nick = nick[:2] + '*' * (len(nick) - 2)
            else:
                hidden_nick = '*' * len(nick) if nick else '***'
            logger.info(f"[房间] 已登录「{hidden_nick}」")
            if expire_date:
                logger.info(f"[房间] Cookie 有效期至 {expire_date}")
        elif has_cookie:
            logger.warning("[房间] Cookie 中存在 sessionid，但服务端返回未登录状态，"
                           "cookie 可能已过期，请重新从浏览器导出")
            logger.info("[房间] 以游客模式采集（礼物等信息可能受限）")
        else:
            logger.info("[房间] 无登录凭证，以游客模式采集（礼物等信息可能受限）")
        return self._ttwid

    # ── 启动 / 停止 ──────────────────────────────

    @property
    def stop_reason(self):
        """停止原因: ''=正常, 'room_not_found'=房间不存在。"""
        return self._stop_reason

    def start(self):
        """启动采集，进入 WebSocket 连接主循环。"""
        logger.debug(f"[启动] live_id: {self.live_id}")
        logger.debug(f"[启动] UA: {self._ua}")
        logger.debug(f"[启动] user_unique_id: {self._user_unique_id}")
        logger.debug(f"[启动] 网络配置: http_timeout={self.HTTP_TIMEOUT}s, "
                     f"ws_connect_timeout={self.WS_CONNECT_TIMEOUT}s, "
                     f"silence_timeout={self.SILENCE_TIMEOUT}s, "
                     f"heartbeat_interval={self.HEARTBEAT_INTERVAL}s, "
                     f"rcvbuf={self.RCVBUF_KB}KB")
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
        self._close_ws()
        for t in (self._heartbeat_thread, self._watchdog_thread, self._stats_thread):
            if t and t.is_alive():
                t.join(timeout=3)
        logger.info(f"[统计] 最终: {self._counter.report()}")
        if self._data_recorder:
            self._data_recorder.close()
        self._stop_recording()
        # 多实例共享 QueueHandler，不在此处关闭（由进程退出统一清理）
        # 单实例模式下 stop() 后进程通常也退出，无需显式关闭

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
        self._queue_handler.set_room_status(
            self.live_id, 'waiting',
            anchor=self.display_name,
            interval=poll_interval,
        )
        self._counter = ThroughputCounter()
        self._reset_recorder()
        self._close_ws()
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
        # 锁内: 快速操作 (video_recorder 持锁,避免与看门狗录制启动竞争)
        with self._recording_lock:
            if self._video_recorder:
                try:
                    self._video_recorder.stop()
                except Exception as e:
                    logger.debug(f"[录制] 停止录制异常: {e}")
                self._video_recorder = None
        # 锁外: 慢操作 (data_recorder 内部已线程安全,close 5s 不持锁)
        if self._data_recorder:
            try:
                self._data_recorder.close()
            except Exception as e:
                logger.debug(f"[数据] 关闭旧 recorder 异常: {e}")
        self._data_recorder = DataRecorder(self.anchor_name, self.live_id, self.config)

    # ── 录制管理 ────────────────────────────────

    def _start_recording(self):
        """启动视频录制（如果启用）。v2: 不再调用 provider 二次拉取地址。"""
        with self._recording_lock:
            self._start_recording_locked()

    def _start_recording_locked(self):
        """_start_recording 的内部实现，调用方需持有 _recording_lock。"""
        if not self._record_cfg.get('enabled', False):
            return
        with self._room_info_lock:
            if not self._room_info:
                return
        if self._stop_event.is_set():
            return
        # 已经在录了就不重复启动
        if self._video_recorder and self._video_recorder.is_recording:
            return
        # 30s 背压：避免 ffmpeg 反复崩时看门狗热循环重试
        self._last_recording_attempt = time.time()

        # 推流 URL 过期修复: ffmpeg 崩了说明 URL 可能已过期
        # 重试用旧的 _room_info 会一直失败 → 刷新一次拿新 URL
        with self._room_info_lock:
            stale = self._room_info_stale
        if stale:
            try:
                new_info = self.query_room_api()
                status = new_info.get('status')
                if status != 2:
                    logger.info(f"[录制] API 状态非开播 (status={status})，跳过重试")
                    return
                with self._room_info_lock:
                    self._room_info = new_info
                    self._room_id = new_info['room_id']
                    self._room_info_stale = False
                logger.info("[录制] 推流地址已刷新（ffmpeg 上次失败后重查 API）")
            except Exception as e:
                logger.warning(f"[录制] 刷新 room_info 失败: {e}，用旧 URL 重试")

        with self._room_info_lock:
            room_info = dict(self._room_info) if self._room_info else {}
        stream_info = select_stream_url(
            room_info,
            quality_name=self._record_cfg.get('quality', '原画'),
            check_health=True,
        )
        if not stream_info.get('is_live') or not stream_info.get('record_url'):
            logger.warning(f"[录制] {self.display_name} 无可用推流地址")
            return

        output_dir = self.config.get('output_dir', 'data')
        barrage_cfg = self.config.get('barrage', {})
        record_local = barrage_cfg.get('local_first', False)
        # 使用弹幕数据的会话目录，让录制文件放在同一目录下
        session_dir = self._data_recorder.session_dir if self._data_recorder else None
        if self._video_recorder is None:
            self._video_recorder = DouyinRecorder(
                self.live_id, self.anchor_name,
                on_failure=self._on_recorder_failure,
                output_dir=output_dir,
                session_dir=session_dir,
                record_local=record_local,
            )
        self._video_recorder.anchor_name = self.anchor_name
        self._video_recorder.start(stream_info['record_url'], self._record_cfg)
        # 只有 ffmpeg 真正起来了才打"画质=xxx 地址=..."和更新面板，避免误导
        if self._video_recorder._recording_active:
            self._stream_url = stream_info['record_url']
            quality = stream_info.get('quality', '?')
            logger.info(f"[录制] {self.display_name} 画质={quality} 地址={stream_info['record_url'][:60]}...")
            self._queue_handler.set_room_status(
                self.live_id, 'collecting',
                anchor=self.display_name,
                msg_count=0,
                elapsed=0,
                rec_elapsed=0,
            )
        else:
            logger.warning(f"[录制] {self.display_name} ffmpeg 未启动成功，将由看门狗重试")

    def _on_recorder_failure(self, return_code):
        """recorder 通知 fetcher ffmpeg 已退出。

        v2 重构：recorder 不再做下播判断和重启，仅通知一次。
        fetcher 清理 recorder 实例，由看门狗（30s 背压后）重新触发 _start_recording。
        """
        with self._recording_lock:
            if self._stop_event.is_set():
                return
            logger.info(f"[录制] ffmpeg 退出 (code={return_code})，看门狗将重试")
            if self._video_recorder is not None:
                try:
                    self._video_recorder.stop()
                except Exception as e:
                    logger.debug(f"[录制] 清理旧 recorder 异常: {e}")
                self._video_recorder = None
            # 记录崩溃时间，下一次看门狗检查会据此施加 30s 背压
            self._last_recording_attempt = time.time()
            # 标记 room_info 过期，下次 _start_recording 会重新查 API 拿新 URL
            with self._room_info_lock:
                self._room_info_stale = True

    def _stop_recording(self):
        """停止视频录制。"""
        with self._recording_lock:
            if self._video_recorder:
                self._video_recorder.stop()
                self._video_recorder = None

    # ── API 方法提取 ──────────────────────────────

    def query_room_api(self):
        """统一调用 enter_room_api，返回房间信息字典。

        替代之前 6 处重复的 enter_room_api(self.ttwid, self._ua, ...) 调用。

        Returns:
            dict: 房间信息（room_id, status, anchor_name, ...）
        Raises:
            RuntimeError: ttwid 获取失败时。
            ValueError: API 返回异常时。
        """
        return enter_room_api(
            self.ttwid, self._ua, self._ua_version,
            self.live_id, self.HTTP_TIMEOUT, session=self.session,
        )

    def refresh_ttwid(self):
        """刷新 ttwid（统一处理 RuntimeError 异常）。

        Returns:
            bool: 刷新成功返回 True，失败返回 False。
        """
        self._ttwid = None
        try:
            _ = self.ttwid
            logger.info("[房间] ttwid 刷新成功")
            return True
        except RuntimeError as e:
            logger.error(f"[房间] ttwid 刷新失败: {e}")
            return False

    def _start_monitor_loop(self):
        """启动等待开播的监控循环（HTTP 轮询 + 状态通知）。

        更新状态面板，由面板统一显示。
        """
        if self._monitor_stop is not None:
            return
        stop_event = threading.Event()
        done_event = threading.Event()
        self._monitor_stop = stop_event
        self._monitor_done = done_event

        poll_interval = self.config.get('live_check_interval', 30)

        def loop():
            try:
                stop_event.wait(5)
                if stop_event.is_set() or self._stop_event.is_set():
                    return
                while not stop_event.is_set() and not self._stop_event.is_set():
                    try:
                        info = self.query_room_api()
                        if info['status'] == 2:
                            with self._room_info_lock:
                                self._room_id = info['room_id']
                                self._room_info = info
                            self._on_live_started(source='api')
                            return
                    except Exception as e:
                        logger.warning(f'[监控] API 检查失败: {e}')
                        if any(kw in str(e).lower() for kw in ('sign', '403', 'unauthorized', 'cookie')):
                            logger.warning(f'[监控] 检测到认证异常，强制刷新 ttwid')
                            self._ttwid = None

                    self._queue_handler.set_room_status(
                        self.live_id, 'waiting',
                        anchor=self.display_name,
                        interval=poll_interval,
                    )
                    for _ in range(int(poll_interval / 0.5)):
                        if stop_event.is_set() or self._stop_event.is_set():
                            break
                        time.sleep(0.5)
            finally:
                # 仅在仍在等待模式时清除状态（_on_live_started 会设 _waiting_live=False）
                if self._is_waiting_live():
                    self._queue_handler.clear_room_status(self.live_id)
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

    def _close_ws(self):
        """安全关闭 WebSocket 连接（线程安全）。"""
        with self._ws_lock:
            if not self.ws:
                return
            try:
                self.ws.keep_running = False
                if self.ws.sock:
                    self.ws.sock.close()
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def _connectWebSocket(self):
        """WebSocket 连接主循环，包含重连逻辑。

        每次重连前：
        1. 重新获取 room_id（主播重开播可能换 ID）
        2. 检查直播状态，未开播时进入等待模式
        3. 刷新 ttwid（签名失败时）
        4. 切换 UA（降低风控）
        5. 指数退避延迟（base × 2^n，封顶 max_delay + 随机抖动）
        """
        max_reconnects = self.MAX_RECONNECTS
        base_delay = self.RECONNECT_BASE_DELAY
        max_delay = self.RECONNECT_MAX_DELAY
        self._reconnect_count = 0

        while not self._stop_event.is_set():
            try:
                logger.info(f"[连接] 第 {self._reconnect_count + 1} 次连接")

                # ── 状态感知（每次重新获取 room_id，主播重开播可能换 ID）──
                with self._room_info_lock:
                    self._room_id = None
                info = self.query_room_api()
                with self._room_info_lock:
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
                    except Exception as e:
                        logger.debug(f"[房间] on_room_info 回调异常: {e}")
                status = info['status']

                if status != 2:
                    status_text = {4: '未开播'}.get(status, f'未知({status})')
                    poll_interval = self.config.get('live_check_interval', 30)
                    # 刷新 ttwid（如果之前标记了需要刷新）
                    if self._ttwid_refresh_needed:
                        self._ttwid_refresh_needed = False
                        if not self.refresh_ttwid():
                            logger.error("[房间] ttwid 刷新失败，无法继续连接，请检查网络")
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
                        if not self.refresh_ttwid():
                            logger.warning("[连接] ttwid 刷新失败，使用现有值继续")
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
                    if not self.refresh_ttwid():
                        logger.error("[房间] ttwid 刷新失败，无法继续连接，请检查网络")
                        break

                # 每次 WebSocket 连接前重新生成 user_unique_id，避免被之前 HTTP 轮询的行为污染
                old_uid = self._user_unique_id
                self._user_unique_id = generate_user_unique_id()
                if self._uid_ever_used:
                    logger.info(f"[连接] user_unique_id 已刷新: {old_uid} → {self._user_unique_id}")
                else:
                    logger.info(f"[连接] 生成 user_unique_id: {self._user_unique_id}")
                    self._uid_ever_used = True

                # 预请求获取服务端 cursor + internalExt（提高连接稳定性）
                cursor, internal_ext = None, None
                try:
                    cursor, internal_ext = fetch_webcast_cursor(
                        self.session, self._room_id, self._user_unique_id,
                        self.ttwid, self._ua, self._login_cookies, self.HTTP_TIMEOUT,
                    )
                    if cursor:
                        logger.info(f"[预请求] 获取 cursor 成功 (长度={len(cursor)})")
                except Exception as e:
                    logger.debug(f"[预请求] 获取 cursor 失败: {e}")

                # 构建 WebSocket URL 并签名
                wss = build_websocket_url(self._room_id, self._user_unique_id, self._ua_version,
                                          cursor=cursor, internal_ext=internal_ext)
                signature = generate_signature(self._room_id, self._user_unique_id)
                if not signature:
                    logger.error("[签名] X-Bogus 签名生成失败，Node.js 未安装或 sign.js 异常，停止采集")
                    break
                wss += f"&signature={signature}"
                self._ws_url = wss
                logger.debug(f"[签名] 生成: signature='{signature}', 长度={len(signature)}, "
                             f"user_unique_id={self._user_unique_id}, room_id={self._room_id}")

                headers = {
                    "cookie": build_ws_cookie(self.ttwid, self._login_cookies),
                    "user-agent": self._ua,
                    "Pragma": "no-cache",
                    "Cache-Control": "no-cache",
                    "Upgrade": "websocket",
                    "Connection": "Upgrade",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
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

                # socket 超时已在模块初始化时通过 setdefaulttimeout(30) 全局设置
                # run_forever 内部每次重连都会读取 getdefaulttimeout()
                # 临时设置全局 socket 超时（websocket-client 1.x 不支持 run_forever timeout 参数）
                import socket as _socket
                _old_timeout = _socket.getdefaulttimeout()
                _socket.setdefaulttimeout(self.WS_CONNECT_TIMEOUT)
                try:
                    self.ws.run_forever(
                        sockopt=((SOL_SOCKET, SO_RCVBUF, (self.RCVBUF_KB * 1024)),),
                        ping_interval=0,
                        ping_timeout=10,
                        origin='https://live.douyin.com',
                    )
                finally:
                    _socket.setdefaulttimeout(_old_timeout)

            except RuntimeError as e:
                logger.error(f"[连接] WebSocket 不可恢复错误，停止采集: {e}")
                break
            except RoomNotFoundError as e:
                logger.error(f"[房间] {e}，停止采集")
                self._stop_reason = 'room_not_found'
                break
            except ValueError as e:
                err_str = str(e)
                if '4001038' in err_str or 'API 响应非 JSON' in err_str:
                    logger.error(f"[房间] 直播间无效（live_id={self.live_id}），停止采集: {e}")
                    break
                logger.error(f"[网络] API 异常: {e}")
            except (OSError, websocket.WebSocketException) as e:
                logger.error(f"[连接] WebSocket 异常: {e}")

            # ── 统一处理：已进入等待模式（消息处理器触发）──
            if self._is_waiting_live():
                while not self._stop_event.is_set():
                    if self._live_event.wait(timeout=1.0):
                        break
                if self._stop_event.is_set():
                    break
                self._live_event.clear()
                self._reconnect_count = 0
                continue

            # ── 重连前快速检测直播间状态，下播则直接进入等待模式 ──
            if not self._is_waiting_live() and not self._stop_event.is_set():
                try:
                    check_info = self.query_room_api()
                    if check_info.get('status') != 2:
                        logger.info(f"[连接] 直播间已下播，进入等待模式")
                        self._enter_wait_mode()
                        continue
                except Exception:
                    pass

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

        # 清理录制进程（重连耗尽或不可恢复错误时不会走到 _enter_wait_mode/stop）
        self._stop_recording()
        logger.info("[控制] 采集主循环退出")
        self._queue_handler.clear_room_status(self.live_id)

    # ── 心跳 / 看门狗 / 统计 ─────────────────────

    def _heartbeat_loop(self):
        """心跳线程，每 heartbeat_interval 秒发送二进制心跳包。"""
        conn_stop = self._conn_stop   # 缓存到局部变量，防止 _wsOnOpen 替换后旧线程不退出
        interval = max(self.HEARTBEAT_INTERVAL, 3)
        while not conn_stop.is_set() and not self._stop_event.is_set():
            try:
                if self._connected_event.is_set():
                    ws = self.ws  # 局部快照，防止 close_ws 置 None
                    if ws:
                        ws.send(
                            PushFrame(payload_type="hb")._pb.SerializeToString(),
                            websocket.ABNF.OPCODE_BINARY,
                        )
            except (OSError, websocket.WebSocketException) as e:
                logger.warning(f"[心跳] 心跳线程异常退出: {e}")
                break
            conn_stop.wait(timeout=interval + random.uniform(0, 2))

    def _watchdog_loop(self):
        """健康守护线程（v3 合并版，单一线程做两件事）：

        1. WS 健康检查：检测静默/假活，触发 _close_ws() 进入重连
           - 完全无数据：用 SILENCE_TIMEOUT 强制重连
           - 30s 无业务消息（首次检测）：快速重连过滤假活
           - 60s 无业务消息（后续）：触发重连
        2. 录制自愈：ffmpeg 崩了/启动失败时，30s 背压后自动重启
           - 触发条件：配置开 ∧ WS 健康 ∧ 非等待开播 ∧ 未在录
           - 必须收到过至少一条业务消息（空房间不浪费 ffmpeg）

        为什么不用两个独立线程：
          - 两个线程都要 conn_stop.wait(10s) 唤醒，唤醒时机一样
          - WS 健康检查和录制自愈的"健康"语义相通，合并后意图更清晰
          - 单一线程，单一 wait，单一 30s 背压
        """
        conn_stop = self._conn_stop
        check_interval = max(min(self.SILENCE_TIMEOUT // 3, 10), 3)
        recording_retry_interval = 30.0
        logger.debug(f"[看门狗] 线程启动，检查间隔={check_interval}s，录制背压={recording_retry_interval}s")
        watchdog_start = time.monotonic()
        first_check_done = False
        first_check_timeout = 30.0
        normal_check_timeout = 60.0
        try:
            while not conn_stop.is_set() and not self._stop_event.is_set():
                conn_stop.wait(timeout=check_interval)
                if self._stop_event.is_set() or conn_stop.is_set():
                    break

                # === 1. WS 健康检查 ===
                if not self._connected_event.is_set():
                    elapsed = time.monotonic() - watchdog_start
                    if elapsed > self.SILENCE_TIMEOUT:
                        logger.warning(f"[看门狗] 连接建立超时 ({elapsed:.0f}s)，强制重连")
                        try:
                            if self.ws and self.ws.sock:
                                self.ws.sock.close()
                        except Exception:
                            pass
                        break
                    continue
                if self._last_msg_time <= 0:
                    continue
                with self._last_msg_time_lock:
                    silence = time.time() - self._last_msg_time
                if silence > self.SILENCE_TIMEOUT:
                    logger.warning(f"[看门狗] {silence:.0f}s 无数据 (阈值={self.SILENCE_TIMEOUT}s)，触发重连")
                    self._close_ws()
                    break
                with self._last_business_msg_time_lock:
                    if self._last_business_msg_time > 0:
                        business_silence = time.time() - self._last_business_msg_time
                    else:
                        business_silence = time.time() - getattr(self, '_ws_connected_at', time.time())
                if not first_check_done:
                    if self._last_business_msg_time > 0:
                        first_check_done = True
                    elif business_silence > first_check_timeout:
                        logger.info(f"[看门狗] {business_silence:.0f}s 无业务消息 (首次检测超时)，快速重连")
                        self._close_ws()
                        break
                else:
                    if business_silence > normal_check_timeout:
                        logger.info(f"[看门狗] {business_silence:.0f}s 无业务消息，触发重连")
                        self._close_ws()
                        break

                # === 2. 录制自愈 ===
                if not self._record_cfg.get('enabled', False):
                    continue
                if self._is_waiting_live():
                    continue
                is_recording = bool(
                    self._video_recorder and self._video_recorder.is_recording
                )
                if is_recording:
                    continue
                if time.time() - self._last_recording_attempt < recording_retry_interval:
                    continue
                # 必须收到过业务消息,空房间/未开播不浪费 ffmpeg
                with self._last_business_msg_time_lock:
                    if self._last_business_msg_time <= 0:
                        continue
                logger.info(
                    f"[录制] 检测到应录制但未录制，尝试自动恢复（30s 背压已过）"
                )
                try:
                    self._start_recording()
                except Exception as e:
                    logger.warning(f"[录制] 自愈失败: {e}")
        except Exception as e:
            logger.error(f"[看门狗] 线程异常: {e}")

    def _stats_loop(self):
        """统计线程，每 stats_interval 秒打印吞吐量报告。"""
        conn_stop = self._conn_stop
        while not conn_stop.is_set() and not self._stop_event.is_set():
            conn_stop.wait(timeout=self.STATS_INTERVAL)
            if self._connected_event.is_set() and not self._is_waiting_live():
                logger.info(f"[统计] {self._counter.report()}")

    # ── WebSocket 回调 ────────────────────────────

    def _save_room_info(self):
        """保存主播信息和下载图片（meta.json 不存在时执行）。"""
        with self._room_info_lock:
            if not self._room_info:
                return
            info_snapshot = dict(self._room_info)
        anchor_name = self.anchor_name

        output_dir = self.config.get('output_dir', 'data')
        room_dir = get_anchor_dir(output_dir, anchor_name, self.live_id)
        meta_file = os.path.join(room_dir, 'meta.json')

        if os.path.exists(meta_file):
            return

        try:
            os.makedirs(room_dir, exist_ok=True)

            # 过滤掉推流地址等敏感信息
            safe_info = {k: v for k, v in info_snapshot.items()
                         if k not in ('stream_url',)}
            meta = {
                'live_id': self.live_id,
                'anchor_name': anchor_name,
                **safe_info,
                'saved_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }

            with open(meta_file, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            logger.info(f"[数据] 主播信息已保存: {meta_file}")

            if download_image(self.session, info_snapshot.get('anchor_avatar', ''),
                              os.path.join(room_dir, 'avatar.jpg')):
                logger.info(f"[数据] 主播头像已下载")

            if download_image(self.session, info_snapshot.get('room_cover', ''),
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
        with self._last_business_msg_time_lock:
            self._last_business_msg_time = 0.0
        self._ws_connected_at = time.time()  # 连接建立时间，看门狗用于计算业务沉默

        # 预计算 parser 配置（消息开关 + 格式/行为配置，每连接刷新一次）
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

        # 初始化 recorder（此时 anchor_name 已通过 enter_room_api 获取）
        if self._data_recorder is None:
            self._data_recorder = DataRecorder(self.anchor_name, self.live_id, self.config)
        self._data_recorder.open()

        # 首次连接时保存主播信息和下载图片
        self._save_room_info()

        # WS 握手成功 = 房间存在的证据，立即启动录制。
        # recorder 不再做二次 API 查询(见 service/recorder.py:127),
        # 不存在"开播瞬间 API 抖动误判下播"的问题。
        # 失败则由 on_failure 回调 + 看门狗(30s 背压)负责恢复。
        # 注意:WS 重连时不主动停旧 ffmpeg,因为:
        #   1. 推流地址 ≠ WS 地址,WS 抖动通常不影响推流 TCP
        #   2. ffmpeg 自带 -reconnect_streamed + -reconnect_delay_max 60 自愈
        #   3. 主动重启 = 不必要的空窗 + 文件碎片
        #   4. URL 真变了 → ffmpeg 自然失败 → on_failure → 看门狗按需重启(查新 URL)
        self._start_recording()

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
        except (gzip.BadGzipFile, OSError) as e:
            logger.warning(f"[连接] gzip 损坏，丢弃本帧（可能是丢包/乱序）: {e}")
            return

        try:
            response = parse_proto(Response, decompressed)
        except (ValueError, KeyError, TypeError) as e:
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
            except (OSError, websocket.WebSocketException) as e:
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
                                logger.info(f"[连接] 开始采集 首条业务消息到达: {msg.method} (连接后 {delay:.1f}s, {time.strftime('%H:%M:%S')})")

                    # 每 3 秒刷新一次面板（节流，避免每条消息都更新）
                    now = time.monotonic()
                    if now - self._panel_last >= 3.0:
                        self._panel_last = now
                        elapsed = now - self._counter._start
                        rec_elapsed = self._video_recorder.elapsed if self._video_recorder else 0
                        self._queue_handler.set_room_status(
                            self.live_id, 'collecting',
                            anchor=self.display_name,
                            msg_count=self._counter._count,
                            elapsed=elapsed,
                            rec_elapsed=rec_elapsed,
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
                            # 控制消息(开始/暂停/结束)用 INFO 提示用户,
                            # 普通消息(弹幕/礼物等)保持 DEBUG 避免刷屏
                            if r.get('type') == 'control':
                                logger.info(msg_text)
                            else:
                                logger.debug(msg_text)

                        rec_type = r.get('type', '')
                        rec_data = r.get('data')
                        if rec_type and rec_type != '_log_only' and rec_data and self._data_recorder:
                            self._data_recorder.record(rec_type, rec_data)

                except Exception as e:
                    logger.error(f"[数据] 处理 {msg.method} 失败: {e}")

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
        self._conn_stop.set()  # 停掉看门狗/心跳/统计线程，避免用旧时间计算超时
