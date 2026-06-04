"""录制管理器：FFmpeg 子进程管理，分段录制，自动转码，下播二次确认。

DouyinRecorder 通过 subprocess.Popen 启动 ffmpeg 下载推流，
后台 daemon 线程监控进程健康，异常退出自动重启。

功能：
    - 分段录制（按时长或文件大小自动切片）
    - 文件大小 / 时长限制
    - 录制完成后自动 ts→mp4 转码
    - 下播二次确认（流中断后延迟复查 API，避免网络抖动误判）
"""

import glob
import logging
import os
import subprocess
import threading
import time
from datetime import datetime

from base.utils import sanitize_dir_name, get_anchor_dir

logger = logging.getLogger(__name__)


def check_ffmpeg():
    """检查 ffmpeg 是否可用。"""
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=10)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
        return False


class DouyinRecorder:
    """抖音直播录制管理器。

    通过 subprocess 调用 ffmpeg 下载推流，支持 ts/flv/mp4 封装格式。
    支持分段录制、文件大小/时长限制、自动转码、下播二次确认。

    Args:
        live_id: 直播间 ID。
        anchor_name: 主播昵称（用于目录和文件名）。
        stream_url_provider: 可选回调，返回最新推流地址。
            返回 str = 新地址，False = 已下播，None = 失败保留旧地址。
        live_status_provider: 可选回调，用于下播二次确认。
            返回 True = 仍在直播，False = 确认下播。
    """

    def __init__(self, live_id, anchor_name='', stream_url_provider=None,
                 live_status_provider=None, output_dir='data', session_dir=None):
        self.live_id = live_id
        self.anchor_name = anchor_name
        self._output_dir = output_dir
        self._session_dir = session_dir or ''  # 外部传入的会话目录
        self._process = None
        self._record_url = ''
        self._record_cfg = {}
        self._save_path = ''
        self._stop_event = threading.Event()
        self._monitor_thread = None
        self._recording_active = False  # 录制是否曾启动（stop()时仍需清理）
        self._start_time = 0.0
        self._stream_url_provider = stream_url_provider
        self._live_status_provider = live_status_provider

    @property
    def is_recording(self):
        return self._process is not None and self._process.poll() is None

    @property
    def display_name(self):
        return self.anchor_name or self.live_id

    @property
    def elapsed(self):
        if self._start_time > 0:
            return time.time() - self._start_time
        return 0

    def start(self, stream_url, record_cfg):
        """启动录制。

        Args:
            stream_url: 推流地址。
            record_cfg: 录制配置字典，支持：
                - format: 封装格式 ts/flv/mp4
                - segment_time: 分段时长（秒），0=不分段
                - segment_size: 分段文件大小（MB），0=不限制
                - auto_convert: 录制结束后自动 ts→mp4 转码
                - recheck_delay: 下播二次确认延迟（秒），0=不确认
        """
        if self.is_recording:
            logger.warning(f"[录制] {self.display_name} 已在录制中")
            return

        if not stream_url:
            logger.error(f"[录制] {self.display_name} 推流地址为空")
            return

        self._record_url = stream_url
        self._record_cfg = record_cfg
        self._stop_event.clear()
        if self._start_ffmpeg():
            self._start_monitor()
            self._recording_active = True

    def _build_save_path(self):
        """构建保存路径（基础路径，不含分段序号）。"""
        fmt = self._record_cfg.get('format', 'ts')
        now = datetime.now()
        ms = now.microsecond // 1000
        if not self._session_dir:
            ts = now.strftime('%Y%m%d_%H%M')
            self._session_dir = os.path.join(
                get_anchor_dir(self._output_dir, self.anchor_name, self.live_id), ts)
            os.makedirs(self._session_dir, exist_ok=True)

        dir_name = sanitize_dir_name(self.anchor_name) or self.live_id
        ts_ms = now.strftime('%Y%m%d_%H%M') + f'_{ms:03d}'
        filename = f"{dir_name}_{ts_ms}.{fmt}"
        return os.path.join(self._session_dir, filename)

    def _start_ffmpeg(self):
        """启动 ffmpeg 进程（支持分段录制）。

        Returns:
            bool: 进程成功启动返回 True。
        """
        if self._stream_url_provider and not self._stop_event.is_set():
            fresh_url = self._stream_url_provider()
            if fresh_url is False:
                logger.info(f"[录制] {self.display_name} 已下播，停止录制重试")
                self._stop_event.set()
                return False
            if fresh_url:
                self._record_url = fresh_url

        if not self._record_url:
            logger.error(f"[录制] {self.display_name} 推流地址为空，无法启动")
            return False

        self._save_path = self._build_save_path()
        segment_time = self._record_cfg.get('segment_time', 0)
        segment_size = self._record_cfg.get('segment_size', 0)

        user_agent = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36')

        cmd = [
            'ffmpeg', '-y',
            '-v', 'quiet',
            '-hide_banner',
            '-user_agent', user_agent,
            '-protocol_whitelist', 'rtmp,crypto,file,http,https,tcp,tls,udp,rtp,httpproxy',
            '-thread_queue_size', '1024',
            '-analyzeduration', '20000000',
            '-probesize', '10000000',
            '-fflags', '+discardcorrupt',
            '-re', '-i', self._record_url,
            '-bufsize', '8000k',
            '-sn', '-dn',
            '-reconnect_delay_max', '60',
            '-reconnect_streamed',
            '-reconnect_at_eof',
            '-max_muxing_queue_size', '1024',
            '-correct_ts_overflow', '1',
            '-avoid_negative_ts', '1',
        ]

        fmt = self._record_cfg.get('format', 'ts')

        # 分段录制：仅 ts 和 mp4 支持 -f segment，需要 segment_time > 0
        use_segment = segment_time > 0 and fmt in ('ts', 'mp4')

        # segment_size > 0 时用 -fs 限制文件大小（分段模式下限制每段，非分段限制总大小）
        if segment_size > 0:
            cmd.extend(['-fs', f'{segment_size}M'])

        if use_segment:
            # 分段模式：输出用 segment muxer
            base_no_ext = os.path.splitext(self._save_path)[0]
            pattern = f'{base_no_ext}_%03d.{fmt}'
            cmd.extend(['-c:v', 'copy', '-c:a', 'copy'])

            seg_mux_args = [
                '-f', 'segment',
                '-segment_time', str(segment_time if segment_time > 0 else 3600),
                '-segment_format', fmt,
                '-reset_timestamps', '1',
            ]
            cmd.extend(seg_mux_args)
            cmd.append(pattern)
        elif fmt == 'flv':
            cmd.extend(['-c:v', 'copy', '-c:a', 'copy', '-bsf:a', 'aac_adtstoasc',
                        '-map', '0', '-f', 'flv', self._save_path])
        elif fmt == 'mp4':
            cmd.extend(['-c:v', 'copy', '-c:a', 'copy',
                        '-movflags', 'frag_keyframe+empty_moov',
                        '-map', '0', '-f', 'mp4', self._save_path])
        else:
            cmd.extend(['-c:v', 'copy', '-c:a', 'copy',
                        '-map', '0', '-f', 'mpegts', self._save_path])

        if use_segment:
            logger.info(f"[录制] {self.display_name} 开始分段录制 → {pattern}")
        else:
            logger.info(f"[录制] {self.display_name} 开始录制 → {self._save_path}")
        if segment_time > 0:
            logger.info(f"[录制] 分段时长: {segment_time}s")
        if segment_size > 0:
            logger.info(f"[录制] 分段大小: {segment_size}MB")

        try:
            self._process = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._start_time = time.time()
            return True
        except FileNotFoundError:
            logger.error("[录制] 未找到 ffmpeg，请安装 FFmpeg")
            self._process = None
        except Exception as e:
            logger.error(f"[录制] 启动 ffmpeg 失败: {e}")
            self._process = None
        return False

    def stop(self):
        """停止录制并等待退出。"""
        was_recording = self._recording_active
        self._stop_event.set()

        # 在清理前保存需要的配置
        auto_convert = self._record_cfg.get('auto_convert', False)

        if was_recording:
            logger.info(f"[录制] {self.display_name} 正在停止...")
            # 尝试优雅停止 ffmpeg（如果进程还活着）
            if self._process and self._process.poll() is None:
                try:
                    if self._process.stdin:
                        self._process.stdin.write(b'q\n')
                        self._process.stdin.close()
                except Exception:
                    try:
                        self._process.terminate()
                    except Exception:
                        pass

                try:
                    self._process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    logger.warning(f"[录制] ffmpeg 未响应，强制终止")
                    try:
                        self._process.kill()
                        self._process.wait(timeout=3)
                    except Exception:
                        pass

            duration = time.time() - self._start_time if self._start_time > 0 else 0
            elapsed = time.strftime('%H:%M:%S', time.gmtime(duration))
            logger.info(f"[录制] {self.display_name} 录制完成，时长 {elapsed}")
            logger.info(f"[录制] 目录: {self._session_dir}")

        self._process = None
        self._record_url = ''
        self._record_cfg = {}
        self._recording_active = False
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=15)

        # 自动转码（即使 ffmpeg 已异常退出，只要有录制曾启动就执行）
        if was_recording and auto_convert:
            self._convert_ts_to_mp4()

    def _start_monitor(self):
        """启动后台监控线程，ffmpeg 异常退出后自动重启。"""
        if self._monitor_thread and self._monitor_thread.is_alive():
            return

        def loop():
            restart_delay = 5
            max_restart_delay = 120
            while not self._stop_event.is_set():
                proc = self._process
                if proc and proc.poll() is not None:
                    return_code = proc.returncode
                    self._process = None
                    if self._stop_event.is_set():
                        if return_code not in (0, 255):
                            logger.warning(f"[录制] {self.display_name} 进程退出 (code={return_code})")
                        break

                    # ── 下播二次确认 ──
                    recheck_delay = self._record_cfg.get('recheck_delay', 10)
                    if recheck_delay > 0 and return_code not in (0,):
                        confirmed = self._recheck_live_status(recheck_delay)
                        if self._stop_event.is_set():
                            break
                        if confirmed is False:
                            logger.info(f"[录制] {self.display_name} 二次确认已下播，停止录制")
                            self._stop_event.set()
                            break
                        elif confirmed is True:
                            logger.info(f"[录制] {self.display_name} 二次确认仍在直播，立即重连")
                        else:
                            logger.info(f"[录制] {self.display_name} 二次确认失败（API异常），按原策略重连")

                    if return_code not in (0, 255):
                        logger.warning(f"[录制] {self.display_name} 进程异常退出 (code={return_code})，{restart_delay}s 后重启")
                    else:
                        logger.info(f"[录制] {self.display_name} 进程退出 (code={return_code})，{restart_delay}s 后重启")
                    self._stop_event.wait(timeout=restart_delay)
                    if self._stop_event.is_set():
                        break
                    if not self._start_ffmpeg():
                        restart_delay = min(restart_delay * 2, max_restart_delay)
                    else:
                        restart_delay = 5
                    continue
                self._stop_event.wait(timeout=5)

        self._monitor_thread = threading.Thread(
            target=loop, daemon=True, name=f'rec-monitor-{self.live_id}'
        )
        self._monitor_thread.start()

    def _recheck_live_status(self, delay):
        """下播二次确认：延迟后复查直播状态。

        Args:
            delay: 延迟秒数。

        Returns:
            True  = 确认仍在直播（网络抖动）。
            False = 确认已下播。
            None  = 无法确认（API 异常）。
        """
        logger.info(f"[录制] {self.display_name} 流中断，{delay}s 后复查直播状态...")
        time.sleep(delay)

        if self._stop_event.is_set():
            return None

        if self._live_status_provider:
            try:
                is_live = self._live_status_provider()
                return is_live
            except Exception as e:
                logger.debug(f"[录制] 下播确认 API 调用失败: {e}")
                return None

        # 无 provider 时尝试用 stream_url_provider
        if self._stream_url_provider:
            result = self._stream_url_provider()
            if result is False:
                return False
            elif result and isinstance(result, str):
                return True
            return None

        return None

    def _convert_ts_to_mp4(self):
        """将录制的 ts 文件自动转码为 mp4，转码成功后删除原 ts 文件。

        安全策略（参考 DouyinLiveRecorder）：
            1. 检查源文件存在且非空
            2. ffmpeg 完成且返回码为 0 才算成功
            3. sleep(1) 确保文件句柄释放后再删除
            4. 删除前再次确认 mp4 存在且 ts 仍存在
        """
        if not self._session_dir or not os.path.isdir(self._session_dir):
            return

        ts_files = sorted(glob.glob(os.path.join(self._session_dir, '*.ts')))
        if not ts_files:
            logger.debug(f"[转码] 无 ts 文件需要转换")
            return

        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
        except FileNotFoundError:
            logger.warning("[转码] ffmpeg 未安装，跳过自动转码")
            return

        logger.info(f"[转码] 开始转换 {len(ts_files)} 个 ts 文件 → mp4")
        converted = 0
        deleted = 0

        for ts_file in ts_files:
            # 安全检查：源文件必须存在且非空
            if not os.path.exists(ts_file) or os.path.getsize(ts_file) == 0:
                logger.debug(f"[转码] 跳过空文件或不存在: {os.path.basename(ts_file)}")
                continue

            mp4_file = ts_file.rsplit('.', 1)[0] + '.mp4'
            if os.path.exists(mp4_file):
                logger.debug(f"[转码] 跳过已存在: {os.path.basename(mp4_file)}")
                continue

            try:
                result = subprocess.run(
                    ['ffmpeg', '-y', '-v', 'quiet', '-hide_banner',
                     '-i', ts_file,
                     '-c', 'copy',
                     '-movflags', '+faststart',
                     '-f', 'mp4',
                     mp4_file],
                    capture_output=True, timeout=300,
                )
                if result.returncode == 0 and os.path.exists(mp4_file) and os.path.getsize(mp4_file) > 0:
                    converted += 1
                    logger.info(f"[转码] 完成: {os.path.basename(mp4_file)}")
                    # 安全删除原 ts：等 1 秒确保文件句柄释放
                    time.sleep(1)
                    if os.path.exists(ts_file) and os.path.exists(mp4_file):
                        try:
                            os.remove(ts_file)
                            deleted += 1
                            logger.debug(f"[转码] 已删除: {os.path.basename(ts_file)}")
                        except OSError as e:
                            logger.warning(f"[转码] 删除失败: {os.path.basename(ts_file)}: {e}")
                else:
                    logger.warning(f"[转码] 失败 (code={result.returncode}): {os.path.basename(ts_file)}")
                    # 转码失败的 mp4 残留文件清理
                    if os.path.exists(mp4_file) and os.path.getsize(mp4_file) == 0:
                        os.remove(mp4_file)
            except subprocess.TimeoutExpired:
                logger.warning(f"[转码] 超时: {os.path.basename(ts_file)}")
            except Exception as e:
                logger.warning(f"[转码] 异常: {e}")

        logger.info(f"[转码] 完成: 转换 {converted} 个, 删除 {deleted} 个 ts 文件, 目录: {self._session_dir}")