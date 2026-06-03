1|"""录制管理器：FFmpeg 子进程管理，分段录制，自动转码，下播二次确认。
2|
3|DouyinRecorder 通过 subprocess.Popen 启动 ffmpeg 下载推流，
4|后台 daemon 线程监控进程健康，异常退出自动重启。
5|
6|功能：
7|    - 分段录制（按时长或文件大小自动切片）
8|    - 文件大小 / 时长限制
9|    - 录制完成后自动 ts→mp4 转码
10|    - 下播二次确认（流中断后延迟复查 API，避免网络抖动误判）
11|"""
12|
13|import glob
14|import logging
15|import os
16|import subprocess
17|import threading
18|import time
from datetime import datetime
19|from datetime import datetime
20|
21|from base.utils import sanitize_dir_name, get_anchor_dir
22|
23|logger = logging.getLogger(__name__)
24|
25|
26|def check_ffmpeg():
27|    """检查 ffmpeg 是否可用。"""
28|    try:
29|        subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=10)
30|        return True
31|    except (FileNotFoundError, subprocess.TimeoutExpired):
32|        return False
33|
34|
35|class DouyinRecorder:
36|    """抖音直播录制管理器。
37|
38|    通过 subprocess 调用 ffmpeg 下载推流，支持 ts/flv/mp4 封装格式。
39|    支持分段录制、文件大小/时长限制、自动转码、下播二次确认。
40|
41|    Args:
42|        live_id: 直播间 ID。
43|        anchor_name: 主播昵称（用于目录和文件名）。
44|        stream_url_provider: 可选回调，返回最新推流地址。
45|            返回 str = 新地址，False = 已下播，None = 失败保留旧地址。
46|        live_status_provider: 可选回调，用于下播二次确认。
47|            返回 True = 仍在直播，False = 确认下播。
48|    """
49|
50|    record_dir = 'recordings'
51|
52|    def __init__(self, live_id, anchor_name='', stream_url_provider=None,
53|                 live_status_provider=None, output_dir='output'):
54|        self.live_id = live_id
55|        self.anchor_name = anchor_name
56|        self._output_dir = output_dir
57|        self._process = None
58|        self._record_url = ''
59|        self._record_cfg = {}
60|        self._save_path = ''
61|        self._session_dir = ''
62|        self._stop_event = threading.Event()
63|        self._monitor_thread = None
64|        self._start_time = 0.0
65|        self._segment_index = 0
66|        self._stream_url_provider = stream_url_provider
67|        self._live_status_provider = live_status_provider
68|
69|    @property
70|    def is_recording(self):
71|        return self._process is not None and self._process.poll() is None
72|
73|    @property
74|    def display_name(self):
75|        return self.anchor_name or self.live_id
76|
77|    @property
78|    def elapsed(self):
79|        if self._start_time > 0:
80|            return time.time() - self._start_time
81|        return 0
82|
83|    def start(self, stream_url, record_cfg):
84|        """启动录制。
85|
86|        Args:
87|            stream_url: 推流地址。
88|            record_cfg: 录制配置字典，支持：
89|                - format: 封装格式 ts/flv/mp4
90|                - segment_time: 分段时长（秒），0=不分段
91|                - segment_size: 分段文件大小（MB），0=不限制
92|                - auto_convert: 录制结束后自动 ts→mp4 转码
93|                - recheck_delay: 下播二次确认延迟（秒），0=不确认
94|        """
95|        if self.is_recording:
96|            logger.warning(f"[录制] {self.display_name} 已在录制中")
97|            return
98|
99|        if not stream_url:
100|            logger.error(f"[录制] {self.display_name} 推流地址为空")
101|            return
102|
103|        self._record_url = stream_url
104|        self._record_cfg = record_cfg
105|        self._stop_event.clear()
106|        self._segment_index = 0
107|        if self._start_ffmpeg():
108|            self._start_monitor()
109|
110|    def _build_save_path(self):
111|        """构建当前分段的保存路径。"""
112|        fmt = self._record_cfg.get('format', 'ts')
113|        now = datetime.now()
114|        ms = now.microsecond // 1000
115|        if self._segment_index == 0:
116|            ts = now.strftime('%Y%m%d_%H%M')
117|            self._session_dir = os.path.join(
118|                get_anchor_dir(self._output_dir, self.anchor_name, self.live_id), ts)
119|            os.makedirs(self._session_dir, exist_ok=True)
120|
121|        seg = self._segment_index
122|        self._segment_index += 1
123|        dir_name = sanitize_dir_name(self.anchor_name) or self.live_id
124|        use_segment = self._record_cfg.get('segment_time', 0) or self._record_cfg.get('segment_size', 0)
125|        ts_ms = now.strftime('%Y%m%d_%H%M') + f'_{ms:03d}'
126|        if use_segment:
127|            filename = f"{dir_name}_{ts_ms}_{seg:03d}.{fmt}"
128|        else:
129|            filename = f"{dir_name}_{ts_ms}.{fmt}"
130|        return os.path.join(self._session_dir, filename)
131|
132|    def _start_ffmpeg(self):
133|        """启动 ffmpeg 进程（支持分段录制）。
134|
135|        Returns:
136|            bool: 进程成功启动返回 True。
137|        """
138|        if self._stream_url_provider and not self._stop_event.is_set():
139|            fresh_url = self._stream_url_provider()
140|            if fresh_url is False:
141|                logger.info(f"[录制] {self.display_name} 已下播，停止录制重试")
142|                self._stop_event.set()
143|                return False
144|            if fresh_url:
145|                self._record_url = fresh_url
146|
147|        if not self._record_url:
148|            logger.error(f"[录制] {self.display_name} 推流地址为空，无法启动")
149|            return False
150|
151|        self._save_path = self._build_save_path()
152|        segment_time = self._record_cfg.get('segment_time', 0)
153|        segment_size = self._record_cfg.get('segment_size', 0)
154|
155|        user_agent = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
156|                      '(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36')
157|
158|        cmd = [
159|            'ffmpeg', '-y',
160|            '-v', 'quiet',
161|            '-hide_banner',
162|            '-user_agent', user_agent,
163|            '-protocol_whitelist', 'rtmp,crypto,file,http,https,tcp,tls,udp,rtp,httpproxy',
164|            '-thread_queue_size', '1024',
165|            '-analyzeduration', '20000000',
166|            '-probesize', '10000000',
167|            '-fflags', '+discardcorrupt',
168|            '-re', '-i', self._record_url,
169|            '-bufsize', '8000k',
170|            '-sn', '-dn',
171|            '-reconnect_delay_max', '60',
172|            '-reconnect_streamed',
173|            '-reconnect_at_eof',
174|            '-max_muxing_queue_size', '1024',
175|            '-correct_ts_overflow', '1',
176|            '-avoid_negative_ts', '1',
177|        ]
178|
179|        fmt = self._record_cfg.get('format', 'ts')
180|
181|        # 分段录制：仅 ts 和 mp4 支持 -f segment
182|        use_segment = (segment_time > 0 or segment_size > 0) and fmt in ('ts', 'mp4')
183|
184|        # 非分段模式下才用 -fs 限制文件大小
185|        if not use_segment and segment_size > 0:
186|            cmd.extend(['-fs', f'{segment_size}M'])
187|
188|        if use_segment:
189|            # 分段模式：输出用 segment muxer
190|            pattern = os.path.join(self._session_dir,
191|                                   f'{sanitize_dir_name(self.anchor_name) or self.live_id}_%03d.{fmt}')
192|            cmd.extend(['-c:v', 'copy', '-c:a', 'copy'])
193|
194|            seg_mux_args = [
195|                '-f', 'segment',
196|                '-segment_time', str(segment_time if segment_time > 0 else 3600),
197|                '-segment_format', fmt,
198|                '-reset_timestamps', '1',
199|            ]
200|            if segment_size > 0:
201|                seg_mux_args.extend(['-segment_size', f'{segment_size}M'])
202|            cmd.extend(seg_mux_args)
203|            cmd.append(pattern)
204|        elif fmt == 'flv':
205|            cmd.extend(['-c:v', 'copy', '-c:a', 'copy', '-bsf:a', 'aac_adtstoasc',
206|                        '-map', '0', '-f', 'flv', self._save_path])
207|        elif fmt == 'mp4':
208|            cmd.extend(['-c:v', 'copy', '-c:a', 'copy',
209|                        '-movflags', 'frag_keyframe+empty_moov',
210|                        '-map', '0', '-f', 'mp4', self._save_path])
211|        else:
212|            cmd.extend(['-c:v', 'copy', '-c:a', 'copy',
213|                        '-map', '0', '-f', 'mpegts', self._save_path])
214|
215|        if use_segment:
216|            logger.info(f"[录制] {self.display_name} 开始分段录制 → {pattern}")
217|        else:
218|            logger.info(f"[录制] {self.display_name} 开始录制 → {self._save_path}")
219|        if segment_time > 0:
220|            logger.info(f"[录制] 分段时长: {segment_time}s")
221|        if segment_size > 0:
222|            logger.info(f"[录制] 分段大小: {segment_size}MB")
223|
224|        try:
225|            self._process = subprocess.Popen(
226|                cmd, stdin=subprocess.PIPE,
227|                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
228|            )
229|            self._start_time = time.time()
230|            return True
231|        except FileNotFoundError:
232|            logger.error("[录制] 未找到 ffmpeg，请安装 FFmpeg")
233|            self._process = None
234|        except Exception as e:
235|            logger.error(f"[录制] 启动 ffmpeg 失败: {e}")
236|            self._process = None
237|        return False
238|
239|    def stop(self):
240|        """停止录制并等待退出。"""
241|        was_recording = self.is_recording
242|        self._stop_event.set()
243|
244|        # 在清理前保存需要的配置
245|        auto_convert = self._record_cfg.get('auto_convert', False)
246|
247|        if was_recording:
248|            logger.info(f"[录制] {self.display_name} 正在停止...")
249|            try:
250|                if self._process.stdin:
251|                    self._process.stdin.write(b'q\n')
252|                    self._process.stdin.close()
253|            except Exception:
254|                try:
255|                    self._process.terminate()
256|                except Exception:
257|                    pass
258|
259|            try:
260|                self._process.wait(timeout=10)
261|            except subprocess.TimeoutExpired:
262|                logger.warning(f"[录制] ffmpeg 未响应，强制终止")
263|                try:
264|                    self._process.kill()
265|                    self._process.wait(timeout=3)
266|                except Exception:
267|                    pass
268|
269|            duration = time.time() - self._start_time if self._start_time > 0 else 0
270|            elapsed = time.strftime('%H:%M:%S', time.gmtime(duration))
271|            logger.info(f"[录制] {self.display_name} 录制完成，时长 {elapsed}")
272|            logger.info(f"[录制] 目录: {self._session_dir}")
273|
274|        self._process = None
275|        self._record_url = ''
276|        self._record_cfg = {}
277|        if self._monitor_thread and self._monitor_thread.is_alive():
278|            self._monitor_thread.join(timeout=3)
279|
280|        # 自动转码
281|        if was_recording and auto_convert:
282|            self._convert_ts_to_mp4()
283|
284|    def _start_monitor(self):
285|        """启动后台监控线程，ffmpeg 异常退出后自动重启。"""
286|        if self._monitor_thread and self._monitor_thread.is_alive():
287|            return
288|
289|        def loop():
290|            restart_delay = 5
291|            max_restart_delay = 120
292|            while not self._stop_event.is_set():
293|                proc = self._process
294|                if proc and proc.poll() is not None:
295|                    return_code = proc.returncode
296|                    self._process = None
297|                    if self._stop_event.is_set():
298|                        if return_code not in (0, 255):
299|                            logger.warning(f"[录制] {self.display_name} 进程退出 (code={return_code})")
300|                        break
301|
302|                    # ── 下播二次确认 ──
303|                    recheck_delay = 10
304|                    if recheck_delay > 0 and return_code not in (0,):
305|                        confirmed = self._recheck_live_status(recheck_delay)
306|                        if confirmed is False:
307|                            logger.info(f"[录制] {self.display_name} 二次确认已下播，停止录制")
308|                            self._stop_event.set()
309|                            break
310|                        elif confirmed is True:
311|                            logger.info(f"[录制] {self.display_name} 二次确认仍在直播，立即重连")
312|                        else:
313|                            logger.info(f"[录制] {self.display_name} 二次确认失败（API异常），按原策略重连")
314|
315|                    if return_code not in (0, 255):
316|                        logger.warning(f"[录制] {self.display_name} 进程异常退出 (code={return_code})，{restart_delay}s 后重启")
317|                    else:
318|                        logger.info(f"[录制] {self.display_name} 进程退出 (code={return_code})，{restart_delay}s 后重启")
319|                    self._stop_event.wait(timeout=restart_delay)
320|                    if self._stop_event.is_set():
321|                        break
322|                    if not self._start_ffmpeg():
323|                        restart_delay = min(restart_delay * 2, max_restart_delay)
324|                    else:
325|                        restart_delay = 5
326|                    continue
327|                self._stop_event.wait(timeout=5)
328|
329|        self._monitor_thread = threading.Thread(
330|            target=loop, daemon=True, name=f'rec-monitor-{self.live_id}'
331|        )
332|        self._monitor_thread.start()
333|
334|    def _recheck_live_status(self, delay):
335|        """下播二次确认：延迟后复查直播状态。
336|
337|        Args:
338|            delay: 延迟秒数。
339|
340|        Returns:
341|            True  = 确认仍在直播（网络抖动）。
342|            False = 确认已下播。
343|            None  = 无法确认（API 异常）。
344|        """
345|        logger.info(f"[录制] {self.display_name} 流中断，{delay}s 后复查直播状态...")
346|        time.sleep(delay)
347|
348|        if self._stop_event.is_set():
349|            return None
350|
351|        if self._live_status_provider:
352|            try:
353|                is_live = self._live_status_provider()
354|                return is_live
355|            except Exception as e:
356|                logger.debug(f"[录制] 下播确认 API 调用失败: {e}")
357|                return None
358|
359|        # 无 provider 时尝试用 stream_url_provider
360|        if self._stream_url_provider:
361|            result = self._stream_url_provider()
362|            if result is False:
363|                return False
364|            elif result and isinstance(result, str):
365|                return True
366|            return None
367|
368|        return None
369|
370|    def _convert_ts_to_mp4(self):
371|        """将录制的 ts 文件自动转码为 mp4，转码成功后删除原 ts 文件。
372|
373|        安全策略（参考 DouyinLiveRecorder）：
374|            1. 检查源文件存在且非空
375|            2. ffmpeg 完成且返回码为 0 才算成功
376|            3. sleep(1) 确保文件句柄释放后再删除
377|            4. 删除前再次确认 mp4 存在且 ts 仍存在
378|        """
379|        if not self._session_dir or not os.path.isdir(self._session_dir):
380|            return
381|
382|        ts_files = sorted(glob.glob(os.path.join(self._session_dir, '*.ts')))
383|        if not ts_files:
384|            logger.debug(f"[转码] 无 ts 文件需要转换")
385|            return
386|
387|        try:
388|            subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
389|        except FileNotFoundError:
390|            logger.warning("[转码] ffmpeg 未安装，跳过自动转码")
391|            return
392|
393|        logger.info(f"[转码] 开始转换 {len(ts_files)} 个 ts 文件 → mp4")
394|        converted = 0
395|        deleted = 0
396|
397|        for ts_file in ts_files:
398|            # 安全检查：源文件必须存在且非空
399|            if not os.path.exists(ts_file) or os.path.getsize(ts_file) == 0:
400|                logger.debug(f"[转码] 跳过空文件或不存在: {os.path.basename(ts_file)}")
401|                continue
402|
403|            mp4_file = ts_file.rsplit('.', 1)[0] + '.mp4'
404|            if os.path.exists(mp4_file):
405|                logger.debug(f"[转码] 跳过已存在: {os.path.basename(mp4_file)}")
406|                continue
407|
408|            try:
409|                result = subprocess.run(
410|                    ['ffmpeg', '-y', '-v', 'quiet', '-hide_banner',
411|                     '-i', ts_file,
412|                     '-c', 'copy',
413|                     '-movflags', '+faststart',
414|                     '-f', 'mp4',
415|                     mp4_file],
416|                    capture_output=True, timeout=300,
417|                )
418|                if result.returncode == 0 and os.path.exists(mp4_file) and os.path.getsize(mp4_file) > 0:
419|                    converted += 1
420|                    logger.info(f"[转码] 完成: {os.path.basename(mp4_file)}")
421|                    # 安全删除原 ts：等 1 秒确保文件句柄释放
422|                    time.sleep(1)
423|                    if os.path.exists(ts_file) and os.path.exists(mp4_file):
424|                        try:
425|                            os.remove(ts_file)
426|                            deleted += 1
427|                            logger.debug(f"[转码] 已删除: {os.path.basename(ts_file)}")
428|                        except OSError as e:
429|                            logger.warning(f"[转码] 删除失败: {os.path.basename(ts_file)}: {e}")
430|                else:
431|                    logger.warning(f"[转码] 失败 (code={result.returncode}): {os.path.basename(ts_file)}")
432|                    # 转码失败的 mp4 残留文件清理
433|                    if os.path.exists(mp4_file) and os.path.getsize(mp4_file) == 0:
434|                        os.remove(mp4_file)
435|            except subprocess.TimeoutExpired:
436|                logger.warning(f"[转码] 超时: {os.path.basename(ts_file)}")
437|            except Exception as e:
438|                logger.warning(f"[转码] 异常: {e}")
439|
440|        logger.info(f"[转码] 完成: 转换 {converted} 个, 删除 {deleted} 个 ts 文件, 目录: {self._session_dir}")