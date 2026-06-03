1|"""采集器主类：WebSocket 连接管理、消息分发、心跳、看门狗、等待开播。
2|
3|DouyinBarrage 是整个采集流程的协调中心，组合以下模块：
4|    base.parser      消息解析与分发表
5|    base.utils       配置加载、Cookie、工具函数
6|    base.output      日志、数据记录
7|    service.network  HTTP 请求、WebSocket 构建、房间 API
8|    service.signer   签名生成
9|
10|线程模型：
11|    主线程    WebSocket 连接循环（含重连逻辑）
12|    daemon    心跳线程（每 N 秒发送 hb）
13|    daemon    看门狗线程（检测静默断连）
14|    daemon    统计线程（定时打印吞吐量）
15|    daemon    监控线程（等待开播模式下的 HTTP 轮询）
16|"""
17|
18|import gzip
19|import json
20|import logging
21|import os
22|import random
23|import re
24|import sys
25|import threading
26|import time
27|import urllib.parse
28|from datetime import datetime
29|from socket import SOL_SOCKET, SO_RCVBUF, setdefaulttimeout, getdefaulttimeout
30|
31|import requests
32|from requests.adapters import HTTPAdapter
33|import websocket
34|
35|logging.getLogger('urllib3').setLevel(logging.CRITICAL)
36|
37|from base.messages import PushFrame, Response, parse_proto
38|from base.parser import HANDLERS
39|from base.utils import (
40|    load_config, load_cookies,
41|    USER_AGENTS, LOW_VALUE_TYPES, INTERACTIVE_TYPES, METHOD_TO_CONFIG,
42|    generate_user_unique_id, extract_ua_version,
43|    rotate_ua,
44|)
45|from base.output import setup_logger, DataRecorder, ThroughputCounter, RoomLogFilter
46|from service.network import (
47|    fetch_ttwid, enter_room_api, download_image,
48|    build_http_headers,
49|    build_websocket_url, build_ws_cookie,
50|)
51|from service.signer import generate_signature
52|
53|logger = logging.getLogger(__name__)
54|
55|
56|class DouyinBarrage:
57|    """抖音直播间弹幕数据采集器。
58|
59|    通过 WebSocket 长连接实时获取 13 种消息类型，输出 CSV/SQLite。
60|    支持登录态、自动重连、等待开播、弱网容错。
61|
62|    Attributes:
63|        _DEFAULT_CONFIG: 统一默认配置，与 config.yaml 做浅合并。
64|    """
65|
66|    # ── 硬编码常量 ──
67|    HTTP_TIMEOUT = 15              # HTTP 超时（秒），超时后自动 ×1.5，封顶 60s，最多重试 3 次
68|    WS_CONNECT_TIMEOUT = 30        # WebSocket 底层 socket 超时（秒）
69|    SILENCE_TIMEOUT = 60           # 看门狗静默阈值（秒）
70|    HEARTBEAT_INTERVAL = 10        # 心跳间隔（秒）
71|    RCVBUF_KB = 512                # 接收缓冲区（KB）
72|    MAX_RECONNECTS = 5             # 最大重连次数（0 = 无限）
73|    RECONNECT_BASE_DELAY = 8       # 重连基础延迟（秒），指数退避：8s → 16s → 32s → ...
74|    RECONNECT_MAX_DELAY = 120      # 最大重连延迟（秒），退避封顶
75|    STATS_INTERVAL = 60            # 吞吐统计打印间隔（秒）
76|
77|    COOKIE_FILE = 'cookie.txt'     # Cookie 文件路径（硬编码）
78|
79|    # 统一默认配置
80|    _DEFAULT_CONFIG = {
81|        'log_level': 'INFO',
82|        'output': {
83|            'chat': True, 'lucky_bag': True, 'gift': True, 'like': True,
84|            'member': True, 'social': True, 'rank': True, 'stats': True,
85|            'fansclub': True, 'emoji': True, 'room': True, 'roomstats': True,
86|            'control': True,
87|        },
88|        'format': {
89|            'gift_combo_final': False,
90|            'csv': False, 'sqlite': False,
91|            'file_dir': 'data',
92|        },
93|        'live_stop': False,
94|        'live_check_interval': 30,
95|    }
96|
97|    def __init__(self, live_id, config_file='config.yaml', log_level=None, on_room_info=None):
98|        """初始化采集器。
99|
100|        Args:
101|            live_id: 直播间 ID（web_rid）。
102|            config_file: 配置文件路径（默认 config.yaml）。
103|            log_level: 日志级别覆盖（None 时使用配置文件中的值）。
104|            on_room_info: 可选回调，首次获取房间信息后调用。
105|                          签名: on_room_info(room_id: str, anchor_name: str)
106|        """
107|        self._on_room_info = on_room_info
108|        # ── 配置 ──
109|        self.config = load_config(config_file, self._DEFAULT_CONFIG)
110|
111|        # ── 日志 ──
112|        effective_level = (log_level or self.config.get('log_level', 'INFO')).upper()
113|        self._logger, self._queue_handler = setup_logger(
114|            log_dir='logs',
115|            log_level=effective_level,
116|        )
117|
118|        self._enable_outputs = self.config.get('output', {})
119|        self._format_cfg = self.config.get('format', {})
120|
121|        # ── UA（一次选定，全局一致）──
122|        self._ua = random.choice(USER_AGENTS)
123|        self._ua_version = extract_ua_version(self._ua)
124|        self._user_unique_id = generate_user_unique_id()
125|
126|        # 网络超时参数直接使用类常量
127|
128|        # ── HTTP Session ──
129|        self.session = requests.Session()
130|        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=2)
131|        self.session.mount('https://', adapter)
132|        self.session.mount('http://', adapter)
133|        self.session.headers.update(build_http_headers(self._ua, self._ua_version))
134|
135|        # ── 登录 Cookie（硬编码路径）──
136|        self._login_cookies = load_cookies(self.COOKIE_FILE)
137|        if self._login_cookies:
138|            for name, value in self._login_cookies.items():
139|                self.session.cookies.set(name, value, domain='.douyin.com')
140|            has_session = bool(self._login_cookies.get('sessionid') or
141|                               self._login_cookies.get('sessionid_ss'))
142|            if has_session:
143|                logger.info(f"[启动] 已加载 Cookie（{len(self._login_cookies)} 项），包含 sessionid，待连接后验证登录态")
144|            else:
145|                logger.info(f"[启动] 已加载 Cookie（{len(self._login_cookies)} 项），未包含 sessionid，将以游客身份采集")
146|        else:
147|            logger.info("[启动] 未加载 cookie.txt，以游客身份采集（礼物等信息可能受限）")
148|
149|        # ── 直播间 ──
150|        self.live_id = live_id
151|
152|        # ── 连接状态 ──
153|        self.ws = None
154|        self._connected_event = threading.Event()
155|        self._stop_event = threading.Event()
156|        self._conn_stop = threading.Event()
157|
158|        # ── 线程引用 ──
159|        self._heartbeat_thread = None
160|        self._watchdog_thread = None
161|        self._stats_thread = None
162|
163|        # ── 健康检测 ──
164|        self._last_msg_time = 0.0
165|        self._last_msg_time_lock = threading.Lock()
166|
167|        # ── 业务消息健康检测（检测"有数据但无业务消息"的假死状态）──
168|        self._last_business_msg_time = 0.0
169|        self._last_business_msg_time_lock = threading.Lock()
170|        self._ws_connected_at = 0.0  # 连接建立时间，看门狗用于计算业务沉默
171|
172|        # ── 吞吐量 ──
173|        self._counter = ThroughputCounter()
174|
175|        # ── 数据记录器（首次连接后初始化）──
176|        self._recorder = None
177|
178|
179|        # ── 连接重试 ──
180|        self._reconnect_count = 0
181|        self._ttwid_refresh_needed = False
182|
183|        # ── ttwid 缓存 ──
184|        self._ttwid = None
185|        self._login_info = {'is_login': False, 'nickname': '', 'uid': ''}
186|
187|        # ── 房间信息 ──
188|        self._room_id = None
189|        self._room_info = None
190|
191|        # ── 等待开播 ──
192|        self._live_lock = threading.Lock()
193|        self._waiting_live = False
194|        self._live_event = threading.Event()
195|        self._monitor_stop = None
196|        self._monitor_done = None
197|
198|        # ── 预计算 enable_outputs 缓存（_wsOnOpen 中更新）──
199|        self._eo_cached = dict(self._enable_outputs)
200|
201|        # ── 面板刷新节流 ──
202|        self._panel_last = 0.0
203|
204|    @property
205|    def anchor_name(self):
206|        return self._room_info.get('anchor_name', '') if self._room_info else ''
207|
208|    @property
209|    def display_name(self):
210|        """显示用名称：优先主播名，降级为 live_id。"""
211|        return self.anchor_name or self.live_id
212|
213|    # ── 懒加载属性 ────────────────────────────────
214|
215|    @property
216|    def ttwid(self):
217|        """获取 ttwid，首次访问触发 HTTP 请求并缓存。
218|
219|        Side Effects:
220|            首次访问时请求 live.douyin.com 获取 ttwid Cookie，
221|            同时验证登录态（is_login / nickname），输出身份验证日志。
222|            解析 sid_guard Cookie 提取有效期并格式化显示。
223|
224|        Returns:
225|            ttwid 字符串。
226|        """
227|        if self._ttwid:
228|            return self._ttwid
229|        self._ttwid, self._login_info = fetch_ttwid(
230|            self.session, self.live_id,
231|            self._login_cookies, self.HTTP_TIMEOUT,
232|        )
233|        # 登录态判定
234|        has_cookie = bool(self._login_cookies.get('sessionid') or
235|                          self._login_cookies.get('sessionid_ss'))
236|        # 提取 cookie 有效期
237|        expire_date = ''
238|        sid_guard = self._login_cookies.get('sid_guard', '')
239|        if sid_guard:
240|            decoded = urllib.parse.unquote(sid_guard)
241|            parts = decoded.split('|')
242|            if len(parts) >= 4:
243|                # 格式: "Thu, 11-Jun-2026 10:31:57 GMT" → 取日期部分
244|                date_str = parts[3].replace('+', ' ').strip()
245|                # 格式化为年月日: "11-Jun-2026" → "2026-06-11"
246|                m_date = re.search(r'(\d+)-(\w+)-(\d+)', date_str)
247|                if m_date:
248|                    day, mon_str, year = m_date.group(1), m_date.group(2), m_date.group(3)
249|                    months = {'Jan':'01','Feb':'02','Mar':'03','Apr':'04','May':'05','Jun':'06',
250|                              'Jul':'07','Aug':'08','Sep':'09','Oct':'10','Nov':'11','Dec':'12'}
251|                    mon = months.get(mon_str[:3], '00')
252|                    expire_date = f'{year}-{mon}-{day}'
253|
254|        if self._login_info['is_login']:
255|            nick = self._login_info['nickname']
256|            logger.info(f"[房间] 已登录「{nick}」")
257|            if expire_date:
258|                logger.info(f"[房间] Cookie 有效期至 {expire_date}")
259|        elif has_cookie:
260|            logger.warning("[房间] Cookie 中存在 sessionid，但服务端返回未登录状态，"
261|                           "cookie 可能已过期，请重新从浏览器导出")
262|            logger.info("[房间] 以游客模式采集（礼物等信息可能受限）")
263|        else:
264|            logger.info("[房间] 无登录凭证，以游客模式采集（礼物等信息可能受限）")
265|        return self._ttwid
266|
267|    @property
268|    def room_id(self):
269|        """获取直播间真实 room_id，首次访问触发 HTTP 请求。
270|
271|        Side Effects:
272|            首次访问时调用 enter_room_api 获取房间信息，
273|            输出房间状态和主播名称日志。
274|
275|        Returns:
276|            room_id 字符串。
277|        """
278|        if self._room_id:
279|            return self._room_id
280|        self._room_info = enter_room_api(
281|            self.ttwid, self._ua, self._ua_version,
282|            self.live_id, self.HTTP_TIMEOUT, session=self.session,
283|        )
284|        self._room_id = self._room_info['room_id']
285|        status = self._room_info['status']
286|        status_text = {2: '直播中', 4: '未开播'}.get(status, f'未知({status})')
287|        logger.info(f'[房间] room_id={self._room_id}, 状态={status_text}, 主播={self.anchor_name}')
288|        return self._room_id
289|
290|    # ── 启动 / 停止 ──────────────────────────────
291|
292|    def start(self):
293|        """启动采集，进入 WebSocket 连接主循环。"""
294|        logger.debug(f"[启动] live_id: {self.live_id}")
295|        logger.debug(f"[启动] UA: {self._ua}")
296|        logger.debug(f"[启动] user_unique_id: {self._user_unique_id}")
297|        logger.debug(f"[启动] 网络配置: http_timeout={self.HTTP_TIMEOUT}s, "
298|                     f"ws_connect_timeout={self.WS_CONNECT_TIMEOUT}s, "
299|                     f"silence_timeout={self.SILENCE_TIMEOUT}s, "
300|                     f"heartbeat_interval={self.HEARTBEAT_INTERVAL}s, "
301|                     f"rcvbuf={self.RCVBUF_KB}KB")
302|        self._connectWebSocket()
303|
304|    def stop(self):
305|        """停止采集，关闭 WebSocket，停止所有线程，输出最终统计。
306|
307|        幂等操作，重复调用无副作用。
308|        """
309|        if self._stop_event.is_set():
310|            return
311|        logger.info("[控制] 停止采集")
312|        self._stop_event.set()
313|        self._live_event.set()  # 解除主循环在 wait_live 中的阻塞
314|        self._connected_event.clear()
315|        self._stop_monitor_loop()
316|        self._queue_handler.clear_room_status(self.live_id)
317|        if self.ws:
318|            try:
319|                self.ws.keep_running = False
320|                # 强制关闭底层 socket，避免 close() 阻塞在发送 close frame 上
321|                if self.ws.sock:
322|                    self.ws.sock.close()
323|                self.ws.close()
324|            except Exception as e:
325|                logger.debug(f"[连接] WebSocket 关闭异常: {e}")
326|        for t in (self._heartbeat_thread, self._watchdog_thread, self._stats_thread):
327|            if t and t.is_alive():
328|                t.join(timeout=3)
329|        logger.info(f"[统计] 最终: {self._counter.report()}")
330|        if self._recorder:
331|            self._recorder.close()
332|        # 多实例共享 QueueHandler，不在此处关闭（由进程退出统一清理）
333|        # 单实例模式下 stop() 后进程通常也退出，无需显式关闭
334|
335|    # ── 等待开播 ──────────────────────────────────
336|
337|    def _enter_wait_mode(self):
338|        """直播结束，进入等待开播模式。
339|
340|        Side Effects:
341|            重置计数器和数据记录器，关闭当前 WebSocket，
342|            启动 HTTP 轮询监控线程。
343|        """
344|        with self._live_lock:
345|            if self._waiting_live:
346|                return
347|            self._waiting_live = True
348|        poll_interval = self.config.get('live_check_interval', 30)
349|        label = self.display_name
350|        logger.info(f'[控制] {label} 监测中（间隔 {poll_interval}s）')
351|        self._queue_handler.set_room_status(
352|            self.live_id, 'waiting',
353|            anchor=self.display_name,
354|            interval=poll_interval,
355|        )
356|        self._counter = ThroughputCounter()
357|        self._reset_recorder()
358|        if self.ws:
359|            try:
360|                self.ws.keep_running = False
361|                # 强制关闭底层 socket，避免 close() 阻塞
362|                if self.ws.sock:
363|                    self.ws.sock.close()
364|                self.ws.close()
365|            except Exception as e:
366|                logger.debug(f"[连接] 等待模式关闭异常：{e}")
367|
368|        self._start_monitor_loop()
369|
370|    def _is_waiting_live(self):
371|        """检查是否处于等待开播模式。
372|
373|        Returns:
374|            True 表示正在等待开播。
375|        """
376|        with self._live_lock:
377|            return self._waiting_live
378|
379|    def _reset_recorder(self):
380|        """关闭并重建数据记录器（幂等操作）。"""
381|        if self._recorder:
382|            try:
383|                self._recorder.close()
384|            except Exception as e:
385|                logger.debug(f"[数据] 关闭旧 recorder 异常: {e}")
386|        self._recorder = DataRecorder(self.anchor_name, self.live_id, self.config)
387|
388|    def _start_monitor_loop(self):
389|        """启动等待开播的监控循环（HTTP 轮询 + 状态通知）。
390|
391|        更新状态面板，由面板统一显示。
392|        """
393|        if self._monitor_stop is not None:
394|            return
395|        stop_event = threading.Event()
396|        done_event = threading.Event()
397|        self._monitor_stop = stop_event
398|        self._monitor_done = done_event
399|
400|        poll_interval = self.config.get('live_check_interval', 30)
401|
402|        def loop():
403|            try:
404|                stop_event.wait(0.3)
405|                if stop_event.is_set() or self._stop_event.is_set():
406|                    return
407|                while not stop_event.is_set() and not self._stop_event.is_set():
408|                    try:
409|                        info = enter_room_api(
410|                            self.ttwid, self._ua, self._ua_version,
411|                            self.live_id, self.HTTP_TIMEOUT, session=self.session,
412|                        )
413|                        if info['status'] == 2:
414|                            self._room_id = info['room_id']
415|                            self._room_info = info
416|                            self._on_live_started(source='api')
417|                            return
418|                    except Exception as e:
419|                        logger.warning(f'[监控] API 检查失败: {e}')
420|                        if any(kw in str(e).lower() for kw in ('sign', '403', 'unauthorized', 'cookie')):
421|                            logger.warning(f'[监控] 检测到认证异常，强制刷新 ttwid')
422|                            self._ttwid = None
423|
424|                    self._queue_handler.set_room_status(
425|                        self.live_id, 'waiting',
426|                        anchor=self.display_name,
427|                        interval=poll_interval,
428|                    )
429|                    for _ in range(int(poll_interval / 0.5)):
430|                        if stop_event.is_set() or self._stop_event.is_set():
431|                            break
432|                        time.sleep(0.5)
433|            finally:
434|                self._queue_handler.clear_room_status(self.live_id)
435|                done_event.set()
436|                if self._monitor_stop is stop_event:
437|                    self._monitor_stop = None
438|                    self._monitor_done = None
439|
440|        t = threading.Thread(target=loop, daemon=True, name=f'monitor-{self.live_id}')
441|        t.start()
442|
443|    def _stop_monitor_loop(self):
444|        """停止监控循环，最多等待 3 秒。"""
445|        stop = self._monitor_stop
446|        done = self._monitor_done
447|        if stop is not None:
448|            stop.set()
449|        if done is not None:
450|            done.wait(timeout=3)
451|
452|    def _on_live_started(self, source):
453|        """检测到开播，清理等待状态并通知主循环。
454|
455|        Args:
456|            source: 检测来源标识（'api' / 'ws' / 'reconnect'）。
457|        """
458|        with self._live_lock:
459|            if not self._waiting_live:
460|                return
461|            self._waiting_live = False
462|        self._stop_monitor_loop()
463|        self._reset_recorder()
464|        self._counter = ThroughputCounter()
465|        self._reconnect_count = 0
466|        self._live_event.set()
467|        self._queue_handler.set_room_status(
468|            self.live_id, 'collecting',
469|            anchor=self.display_name,
470|            msg_count=0,
471|            elapsed=0,
472|        )
473|        label = self.display_name
474|        logger.info(f'[房间] {label} 已开播')
475|        logger.info(f"[房间] 检测到开播 (来源:{source})，重新连接...")
476|
477|    # ── WebSocket 连接循环 ────────────────────────
478|
479|    def _connectWebSocket(self):
480|        """WebSocket 连接主循环，包含重连逻辑。
481|
482|        每次重连前：
483|        1. 重新获取 room_id（主播重开播可能换 ID）
484|        2. 检查直播状态，未开播时进入等待模式
485|        3. 刷新 ttwid（签名失败时）
486|        4. 切换 UA（降低风控）
487|        5. 指数退避延迟（base × 2^n，封顶 max_delay + 随机抖动）
488|        """
489|        max_reconnects = self.MAX_RECONNECTS
490|        base_delay = self.RECONNECT_BASE_DELAY
491|        max_delay = self.RECONNECT_MAX_DELAY
492|        self._reconnect_count = 0
493|
494|        while not self._stop_event.is_set():
495|            try:
496|                logger.info(f"[连接] 第 {self._reconnect_count + 1} 次连接")
497|
498|                # ── 状态感知（每次重新获取 room_id，主播重开播可能换 ID）──
499|                self._room_id = None
500|                info = enter_room_api(
501|