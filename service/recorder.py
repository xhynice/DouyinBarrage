"""录制管理器：FFmpeg 子进程管理，分段录制，自动转码。

DouyinRecorder 通过 subprocess.Popen 启动 ffmpeg 下载推流，
后台 daemon 线程监控进程健康，异常退出通知 fetcher 看门狗。

功能：
    - 分段录制（按时长或文件大小自动切片）
    - 文件大小 / 时长限制
    - 录制完成后自动 ts→mp4 转码（ffprobe 验证完整性后再删 ts）
"""

__all__ = ['DouyinRecorder', 'check_ffmpeg']

import glob
import logging
import os
import shutil
import subprocess
import tempfile
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
    支持分段录制、文件大小/时长限制、自动转码。

    设计原则（v2 重构）：
        - recorder 不做"是否还在直播"的判断，那是 fetcher 的职责
        - recorder 不主动调用 API 拉流地址（避免与 fetcher 重复查询、状态误判）
        - ffmpeg 异常退出时只通过 on_failure 回调通知外部，由 fetcher 看门狗统一决策

    Args:
        live_id: 直播间 ID。
        anchor_name: 主播昵称（用于目录和文件名）。
        on_failure: 可选回调，签名 (return_code: int) -> None。
            ffmpeg 进程退出时（排除 stop() 主动停止）触发一次，用于通知 fetcher。
        output_dir: 录制输出根目录。
        session_dir: 可选，会话目录（外部传入时与弹幕数据共存）。
    """

    def __init__(self, live_id, anchor_name='', on_failure=None,
                 output_dir='data', session_dir=None, record_local=False):
        self.live_id = live_id
        self.anchor_name = anchor_name
        self._output_dir = output_dir
        self._session_dir = session_dir or ''  # 外部传入的会话目录（持久化）
        self._record_local = record_local      # 先写本地再搬迁
        self._work_dir = None                  # 本地临时目录
        self._process = None
        self._record_url = ''
        self._record_cfg = {}
        self._save_path = ''
        self._stop_event = threading.Event()
        self._monitor_thread = None
        self._recording_active = False  # 录制是否曾启动（stop()时仍需清理）
        self._start_time = 0.0
        self._on_failure = on_failure
        self._ffmpeg_log_fp = None
        self._ffmpeg_log_path = ''

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

    @property
    def session_dir(self):
        """当前录制会话目录（只读）。"""
        return self._session_dir

    def start(self, stream_url, record_cfg):
        """启动录制。

        Args:
            stream_url: 推流地址。
            record_cfg: 录制配置字典，支持：
                - format: 封装格式 ts/flv/mp4
                - segment_time: 分段时长（秒），0=不分段
                - segment_size: 分段文件大小（MB），0=不限制
                - auto_convert: 录制结束后自动 ts→mp4 转码
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

        # 录制文件写入位置：本地模式用 /tmp，否则直接写持久化目录
        if self._record_local:
            if not self._work_dir:
                self._work_dir = tempfile.mkdtemp(prefix=f'douyin_rec_')
            save_dir = self._work_dir
        else:
            os.makedirs(self._session_dir, exist_ok=True)
            save_dir = self._session_dir

        dir_name = sanitize_dir_name(self.anchor_name) or self.live_id
        ts_ms = now.strftime('%Y%m%d_%H%M') + f'_{ms:03d}'
        filename = f"{dir_name}_{ts_ms}.{fmt}"
        return os.path.join(save_dir, filename)

    def _start_ffmpeg(self):
        """启动 ffmpeg 进程（支持分段录制）。

        推流地址由调用方通过 start(stream_url, ...) 传入，
        recorder 不再做二次 API 拉取（避免与 fetcher 重复查询）。

        Returns:
            bool: 进程成功启动返回 True。
        """
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
            '-v', 'error',
            '-hide_banner',
            '-user_agent', user_agent,
            '-protocol_whitelist', 'rtmp,crypto,file,http,https,tcp,tls,udp,rtp,httpproxy',
            '-thread_queue_size', '1024',
            '-analyzeduration', '20000000',
            '-probesize', '10000000',
            '-fflags', '+discardcorrupt',
            '-re', '-i', self._record_url,
        ]

        fmt = self._record_cfg.get('format', 'ts')

        # 分段录制：仅 ts 和 mp4 支持 -f segment，需要 segment_time > 0
        use_segment = segment_time > 0 and fmt in ('ts', 'mp4')

        # segment_size > 0 时用 -fs 限制文件大小（分段模式下限制每段，非分段限制总大小）
        if segment_size > 0:
            cmd.extend(['-fs', f'{segment_size}M'])

        # 通用输出选项（参考 DouyinLiveRecorder，放在编码参数之后）
        common_output_opts = [
            '-bufsize', '8000k',
            '-sn', '-dn',
            '-reconnect_delay_max', '60',
            '-reconnect_streamed', '-reconnect_at_eof',
            '-max_muxing_queue_size', '2048',
            '-correct_ts_overflow', '1',
            '-avoid_negative_ts', '1',
        ]

        if use_segment:
            # 分段模式：输出用 segment muxer
            base_no_ext = os.path.splitext(self._save_path)[0]
            pattern = f'{base_no_ext}_%03d.{fmt}'
            cmd.extend(['-c:v', 'copy', '-c:a', 'copy'])
            cmd.extend(common_output_opts)
            cmd.extend([
                '-f', 'segment',
                '-segment_time', str(segment_time if segment_time > 0 else 3600),
                '-segment_format', fmt,
                '-reset_timestamps', '1',
                pattern,
            ])
        elif fmt == 'flv':
            cmd.extend(['-c:v', 'copy', '-c:a', 'copy', '-bsf:a', 'aac_adtstoasc', '-map', '0'])
            cmd.extend(common_output_opts)
            cmd.extend(['-f', 'flv', self._save_path])
        elif fmt == 'mp4':
            cmd.extend(['-c:v', 'copy', '-c:a', 'copy', '-movflags', 'frag_keyframe+empty_moov', '-map', '0'])
            cmd.extend(common_output_opts)
            cmd.extend(['-f', 'mp4', self._save_path])
        else:
            cmd.extend(['-c:v', 'copy', '-c:a', 'copy', '-map', '0'])
            cmd.extend(common_output_opts)
            cmd.extend(['-f', 'mpegts', self._save_path])

        if use_segment:
            logger.info(f"[录制] {self.display_name} 开始分段录制 → {pattern}")
        else:
            logger.info(f"[录制] {self.display_name} 开始录制 → {self._save_path}")
        if segment_time > 0:
            logger.info(f"[录制] 分段时长: {segment_time}s")
        if segment_size > 0:
            logger.info(f"[录制] 分段大小: {segment_size}MB")

        try:
            # stderr 写入日志文件，方便排查 ffmpeg 错误
            log_dir = os.path.join(self._session_dir or self._output_dir, 'logs')
            os.makedirs(log_dir, exist_ok=True)
            ffmpeg_log = os.path.join(log_dir, f'ffmpeg_{os.getpid()}.log')
            self._ffmpeg_log_path = ffmpeg_log
            self._ffmpeg_log_fp = open(ffmpeg_log, 'w', encoding='utf-8')
            self._process = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=self._ffmpeg_log_fp,
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
                    self._process.wait(timeout=30)
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
            # on_failure 在 monitor 线程内调用时,不能 join 自己
            if self._monitor_thread is not threading.current_thread():
                self._monitor_thread.join(timeout=15)

        # 搬到持久化目录
        if was_recording:
            self._move_to_persistent()

        # 自动转码（即使 ffmpeg 已异常退出，只要有录制曾启动就执行）
        if was_recording and auto_convert:
            self._convert_ts_to_mp4()

    def _start_monitor(self):
        """启动后台监控线程，仅在 ffmpeg 异常退出时通知外部一次。

        v2 重构：移除 ffmpeg 自动重启和"下播二次确认"逻辑。
        录制恢复由 fetcher 看门狗统一调度，避免 recorder 单方面判下播。
        """
        if self._monitor_thread and self._monitor_thread.is_alive():
            return

        def loop():
            while not self._stop_event.is_set():
                proc = self._process
                if proc and proc.poll() is not None:
                    return_code = proc.returncode
                    self._process = None
                    if self._stop_event.is_set():
                        # 主动 stop() 触发的退出，静默返回
                        break
                    # ffmpeg 异常退出：通知 fetcher，由看门狗决定是否重启录制
                    if return_code == 0:
                        logger.info(f"[录制] {self.display_name} 进程正常退出 (code=0)")
                    elif return_code == 255:
                        logger.info(f"[录制] {self.display_name} 进程退出 (code=255)")
                    else:
                        logger.warning(f"[录制] {self.display_name} 进程异常退出 (code={return_code})")
                        # 读取 ffmpeg 错误日志
                        self._close_ffmpeg_log()
                    if self._on_failure:
                        try:
                            self._on_failure(return_code)
                        except Exception as e:
                            logger.debug(f"[录制] on_failure 回调异常: {e}")
                    break
                self._stop_event.wait(timeout=5)

        self._monitor_thread = threading.Thread(
            target=loop, daemon=True, name=f'rec-monitor-{self.live_id}'
        )
        self._monitor_thread.start()

    def _close_ffmpeg_log(self):
        """关闭 ffmpeg 日志文件，读取并输出最后几行错误。"""
        try:
            if self._ffmpeg_log_fp:
                self._ffmpeg_log_fp.close()
                self._ffmpeg_log_fp = None
        except Exception:
            pass
        # 读取日志最后 10 行
        try:
            if hasattr(self, '_ffmpeg_log_path') and os.path.exists(self._ffmpeg_log_path):
                with open(self._ffmpeg_log_path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
                    if lines:
                        last_lines = lines[-10:]
                        for line in last_lines:
                            logger.warning(f"[ffmpeg] {line.rstrip()}")
        except Exception:
            pass

    def _move_to_persistent(self):
        """将录制文件从本地临时目录搬到持久化目录。"""
        if not self._work_dir or not os.path.isdir(self._work_dir):
            return
        try:
            os.makedirs(self._session_dir, exist_ok=True)
            moved = 0
            for name in os.listdir(self._work_dir):
                src = os.path.join(self._work_dir, name)
                dst = os.path.join(self._session_dir, name)
                try:
                    shutil.move(src, dst)
                    moved += 1
                except Exception as e:
                    logger.warning(f"[录制] 搬迁失败 {name}: {e}")
            try:
                shutil.rmtree(self._work_dir, ignore_errors=True)
            except Exception:
                pass
            self._work_dir = None
            if moved:
                logger.info(f"[录制] {moved} 个文件已搬到持久化目录")
        except Exception as e:
            logger.error(f"[录制] 搬迁异常，文件留在 {self._work_dir}: {e}")

    def _convert_ts_to_mp4(self):
        """将录制的 ts 文件自动转码为 mp4，转码成功后删除原 ts 文件。

        安全策略（参考 DouyinLiveRecorder）：
            1. 检查源文件存在且非空
            2. ffmpeg 转码完成 + ffprobe 验证 mp4 完整才算成功
            3. sleep(3) 确保文件句柄释放后再删除 ts
            4. 删除后 sleep(2) 验证 ts 是否真正消失
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
                # mp4 存在但 ts 还在 → 补删 ts
                if os.path.exists(ts_file):
                    try:
                        os.remove(ts_file)
                        time.sleep(2)
                        if not os.path.exists(ts_file):
                            deleted += 1
                            logger.debug(f"[转码] 补删残留 ts: {os.path.basename(ts_file)}")
                    except OSError:
                        pass
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
                if result.returncode != 0 or not os.path.exists(mp4_file) or os.path.getsize(mp4_file) == 0:
                    logger.warning(f"[转码] 失败 (code={result.returncode}): {os.path.basename(ts_file)}")
                    if os.path.exists(mp4_file) and os.path.getsize(mp4_file) == 0:
                        os.remove(mp4_file)
                    continue

                # ffprobe 验证 mp4 完整性
                try:
                    probe = subprocess.run(
                        ['ffprobe', '-v', 'error', '-show_entries',
                         'format=duration', '-of', 'csv=p=0', mp4_file],
                        capture_output=True, timeout=30,
                    )
                    if probe.returncode != 0 or not probe.stdout.strip():
                        logger.warning(f"[转码] mp4 验证失败，保留 ts: {os.path.basename(ts_file)}")
                        os.remove(mp4_file)
                        continue
                except FileNotFoundError:
                    logger.debug("[转码] ffprobe 未安装，跳过完整性验证")

                converted += 1
                logger.info(f"[转码] 完成: {os.path.basename(mp4_file)}")

                # 等待文件句柄释放后删除 ts
                time.sleep(3)
                if os.path.exists(ts_file) and os.path.exists(mp4_file):
                    try:
                        os.remove(ts_file)
                        time.sleep(2)
                        if not os.path.exists(ts_file):
                            deleted += 1
                            logger.debug(f"[转码] 已删除: {os.path.basename(ts_file)}")
                        else:
                            logger.warning(f"[转码] ts 删除后仍存在: {os.path.basename(ts_file)}")
                    except OSError as e:
                        logger.warning(f"[转码] 删除失败: {os.path.basename(ts_file)}: {e}")
            except subprocess.TimeoutExpired:
                logger.warning(f"[转码] 超时: {os.path.basename(ts_file)}")
            except Exception as e:
                logger.warning(f"[转码] 异常: {e}")

        logger.info(f"[转码] 完成: 转换 {converted} 个, 删除 {deleted} 个 ts 文件, 目录: {self._session_dir}")