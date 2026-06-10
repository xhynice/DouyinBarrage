"""输出模块：异步日志、数据记录（CSV / SQLite 批量写入）、吞吐统计。

日志通过 QueueHandler + deque 异步写出，避免阻塞主线程的消息处理。
控制台仅显示 WARNING/ERROR 和状态面板，INFO/DEBUG 仅写入文件。
"""

__all__ = [
    'RoomLogFilter', 'QueueHandler', 'ThroughputCounter', 'DataRecorder',
    'setup_logger', 'get_room_statuses',
]

import csv
import logging
import logging.handlers
import os
import sqlite3
import threading
import time
from collections import deque

from base.utils import SCRIPT_DIR, sanitize_dir_name, get_anchor_dir
class RoomLogFilter(logging.Filter):
    """根据当前线程名自动添加 [主播名] 前缀。

    线程命名规则：room-{live_id} → 日志前缀 [{anchor}]
    未获取到主播名时降级显示 [{live_id}]。
    非房间线程（主线程等）不添加前缀。
    """

    _anchor_map = {}

    @classmethod
    def update_anchor(cls, live_id, anchor):
        if anchor and anchor != live_id:
            cls._anchor_map[live_id] = anchor

    def filter(self, record):
        thread_name = threading.current_thread().name
        if thread_name.startswith('room-'):
            live_id = thread_name[5:]
            label = self._anchor_map.get(live_id, live_id)
            record.msg = f"[{label}] {record.msg}"
        return True
class QueueHandler(logging.Handler):
    
    """异步日志处理器，将日志放入 deque，后台线程批量写出。

    内部使用 maxlen=50000 的 deque 做缓冲，溢出时丢弃新日志并
    在下次刷新时输出丢弃计数。后台线程每 2s 刷新一次。

    - WARNING/ERROR 输出到控制台，INFO/DEBUG 仅写文件
    - 状态面板每 2s 刷新一次，显示所有房间状态
    """

    def __init__(self):
        super().__init__()
        self._buf = deque(maxlen=50_000)
        self._handlers: list[logging.Handler] = []
        self._stop = threading.Event()
        self._dropped = 0
        self._thread = None
        # ── 状态面板 ──
        self._room_status = {}      # {live_id: {status, anchor, ...}}
        self._status_lock = threading.Lock()
        self._shutting_down = False # 退出时设为 True，抑制面板渲染

    def _ensure_started(self):
        """首次添加 handler 时启动后台刷新线程（幂等）。"""
        if self._thread is None:
            self._thread = threading.Thread(target=self._drain_loop, daemon=True, name='log-drain')
            self._thread.start()

    def add_handler(self, h):
        """添加一个下游日志 handler（如 FileHandler、StreamHandler）。

        Args:
            h: logging.Handler 实例。
        """
        self._handlers.append(h)
        self._ensure_started()

    def emit(self, record):
        """将日志记录放入内部缓冲区（非阻塞）。"""
        try:
            self._buf.append(record)
        except Exception:
            self._dropped += 1

    def _drain_loop(self):
        """后台刷新循环，每 10s 将缓冲区日志批量写出。"""
        while not self._stop.is_set():
            self._drain()
            time.sleep(10)
        self._drain()

    def _drain(self):
        """从缓冲区取出最多 500 条日志，分发到所有下游 handler。

        WARNING/ERROR 输出到控制台，INFO/DEBUG 仅写文件。
        """
        batch = []
        while len(batch) < 500:
            try:
                batch.append(self._buf.popleft())
            except IndexError:
                break

        for h in self._handlers:
            for r in batch:
                try:
                    if r.levelno < h.level:
                        continue
                    h.emit(r)
                except Exception as e:
                    logger.debug(f"[日志] handler.emit 异常: {e}")
            try:
                h.flush()
            except Exception:
                pass

        if self._dropped:
            for h in self._handlers:
                if type(h) is not logging.StreamHandler:
                    try:
                        h.emit(logging.LogRecord(
                            'logger', logging.WARNING, '', 0,
                            f'⚠️ 日志缓冲区溢出，已丢弃 {self._dropped} 条', (), None
                        ))
                    except Exception:
                        pass
            self._dropped = 0

    # ── 房间状态面板 ─────────────────────────────

    def set_room_status(self, live_id, status, **info):
        """更新房间状态（线程安全）。

        Args:
            live_id: 直播间 ID。
            status: 'waiting' 或 'collecting'。
            **info: 额外信息（anchor, msg_count, elapsed, interval）。
        """
        with self._status_lock:
            entry = self._room_status.get(live_id, {})
            entry['status'] = status
            entry['_updated'] = time.monotonic()
            entry.update(info)
            self._room_status[live_id] = entry
        anchor = info.get('anchor')
        if anchor:
            RoomLogFilter.update_anchor(live_id, anchor)

    def clear_room_status(self, live_id):
        """移除房间状态（线程安全）。"""
        with self._status_lock:
            self._room_status.pop(live_id, None)

    def get_all_status(self):
        """返回当前所有房间状态的快照（线程安全）。

        Returns:
            {live_id: {status, anchor, msg_count, elapsed, rec_elapsed, ...}}
        """
        with self._status_lock:
            return dict(self._room_status)

    def close(self):
        """停止后台线程，刷新剩余日志，关闭所有下游 handler。"""
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._drain()
        for h in self._handlers:
            try:
                h.close()
            except Exception:
                pass
        super().close()
_shared_queue_handler = None
def get_shared_queue_handler():
    """获取共享的 QueueHandler 实例。"""
    return _shared_queue_handler
def get_room_statuses():
    """获取所有房间最新状态的快照。"""
    qh = _shared_queue_handler
    if qh is None:
        return {}
    return qh.get_all_status()
def _cleanup_old_logs(log_dir, keep_days=7):
    """删除超过 keep_days 天的 .log 文件。"""
    import glob
    now = time.time()
    cutoff = now - keep_days * 86400
    for f in glob.glob(os.path.join(log_dir, '*.log')):
        try:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
        except OSError:
            pass

def setup_logger(log_dir='logs', log_level='INFO'):
    """配置全局 logger，返回 (logger, queue_handler)。

    日志级别为 NONE 时关闭日志输出，但数据文件照常写入。
    控制台默认显示 INFO 及以上级别（跟随 log_level 配置）。
    首次调用创建 handler，后续调用复用已有 handler，仅更新日志级别。

    Args:
        log_dir: 日志文件输出目录。
        log_level: 日志级别，'NONE' 表示关闭日志。

    Returns:
        (logging.Logger, QueueHandler) 元组。
    """
    logger = logging.getLogger()
    log_enabled = log_level.upper() != 'NONE'
    level_name = log_level.upper()
    user_level = getattr(logging, level_name, logging.INFO)

    # 多实例安全：如果已有 handler，说明其他实例已初始化，复用即可
    if logger.handlers:
        if log_enabled:
            current = logger.level or logging.CRITICAL
            if user_level < current:
                logger.setLevel(user_level)
        for h in logger.handlers:
            if isinstance(h, QueueHandler):
                if not any(isinstance(f, RoomLogFilter) for f in h.filters):
                    h.addFilter(RoomLogFilter())
                # 更新控制台级别：取已有级别和新级别的较小值（更详细）
                for sh in h._handlers:
                    if isinstance(sh, logging.StreamHandler) and not isinstance(sh, logging.FileHandler):
                        new_console_level = min(logging.WARNING, user_level)
                        if new_console_level < sh.level:
                            sh.setLevel(new_console_level)
                return logger, h
        queue_handler = QueueHandler()
        queue_handler.addFilter(RoomLogFilter())
        logger.addHandler(queue_handler)
        return logger, queue_handler

    if log_enabled:
        logger.setLevel(user_level)
    else:
        logger.setLevel(logging.CRITICAL + 1)

    queue_handler = QueueHandler()
    queue_handler.addFilter(RoomLogFilter())

    if log_enabled:
        os.makedirs(log_dir, exist_ok=True)
        # 清理 7 天前的日志
        _cleanup_old_logs(log_dir, keep_days=7)
        log_file = os.path.join(log_dir, time.strftime('%Y-%m-%d') + '.log')
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(logging.Formatter(
            '[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%m-%d %H:%M'
        ))
        queue_handler.add_handler(file_handler)

    # 控制台直接输出（不经过 QueueHandler，避免日志延迟）
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter('%(message)s'))
    console.setLevel(user_level)
    logger.addHandler(console)

    global _shared_queue_handler
    _shared_queue_handler = queue_handler
    logger.addHandler(queue_handler)
    return logger, queue_handler
logger = logging.getLogger(__name__)
class ThroughputCounter:
    """消息吞吐量计数器，统计总消息数和按类型分布。

    用于定时打印采集速率（msg/s）和 Top 5 消息类型。
    只计数 enabled 的消息类型。
    """

    __slots__ = ('_count', '_start', '_by_type')

    def __init__(self):
        self._count = 0
        self._start = time.monotonic()
        self._by_type = {}

    def inc(self, msg_type: str = '', enabled: bool = True):
        """递增计数。

        Args:
            msg_type: 消息类型标识（如 'chat'、'gift'），为空时仅计总数。
            enabled: 是否计入统计（False 时跳过）。
        """
        if not enabled:
            return
        self._count += 1
        if msg_type:
            self._by_type[msg_type] = self._by_type.get(msg_type, 0) + 1

    def report(self) -> str:
        """生成统计报告字符串。

        Returns:
            '总计:N | X.Xmsg/s [top5 类型]' 格式的报告。
        """
        elapsed = time.monotonic() - self._start
        if elapsed < 0.1:
            return "统计中..."
        rate = self._count / elapsed
        parts = [f"总计:{self._count}", f"{rate:.1f}msg/s"]
        if self._by_type:
            top = sorted(self._by_type.items(), key=lambda x: -x[1])[:5]
            parts.append("[" + ", ".join(f"{k}:{v}" for k, v in top) + "]")
        return " | ".join(parts)
class DataRecorder:
    """数据记录器，支持 CSV、SQLite 两格式批量写入。

    生命周期：构造 → open() → record() × N → close()
    CSV 文件延迟创建：首次收到某类型数据时才创建文件并写入表头。
    SQLite 数据库位于会话目录（data/{主播名}/{会话}/data.db），与录制文件同级。
    SQLite 的 time 字段存为 Unix 秒级时间戳（INTEGER），用于视频+弹幕同步。
    后台线程每 2s 刷新一次缓冲区（deque 10 万上限，溢出丢弃）。

    Attributes:
        CSV_FIELDS: 各消息类型的字段定义（CSV / SQLite 共用）。
        INTEGER_FIELDS: 各消息类型中应存为 SQLite INTEGER 的字段集合。
    """

    CSV_FIELDS = {
        'chat':      ['time', 'user_id', 'user_name', 'content', 'grade', 'fans_club'],
        'lucky_bag': ['time', 'user_id', 'user_name', 'content', 'grade', 'fans_club'],
        'gift':     ['time', 'user_id', 'user_name', 'gift_name', 'gift_count', 'diamond_total', 'grade', 'fans_club'],
        'like':     ['time', 'user_id', 'user_name', 'count', 'total', 'grade', 'fans_club'],
        'member':   ['time', 'user_id', 'user_name', 'gender', 'grade', 'fans_club', 'member_count'],
        'social':   ['time', 'user_id', 'user_name', 'action', 'follow_count', 'grade', 'fans_club'],
        'fansclub': ['time', 'user_id', 'user_name', 'type', 'content', 'grade', 'fans_club'],
        'emoji':    ['time', 'user_id', 'user_name', 'emoji_id', 'content', 'grade', 'fans_club'],
        'stats':    ['time', 'current', 'total_pv', 'total_user', 'online_anchor'],
        'roomstats':['time', 'detail', 'total'],
        'room':     ['time', 'is_top', 'room_id', 'content', 'biz_scene'],
        'rank':     ['time', 'ranks'],
        'control':  ['time', 'status'],
    }

    # 各消息类型中应存为 SQLite INTEGER 的字段（其余为 TEXT）。
    # 来源：protobuf 原始类型为 INT64 / UINT64 / UINT32 的字段。
    INTEGER_FIELDS = {
        'gift':     {'gift_count', 'diamond_total'},
        'like':     {'count', 'total'},
        'member':   {'member_count'},
        'social':   {'follow_count'},
        'stats':    {'current'},
        'emoji':    {'emoji_id'},
        'roomstats':{'total'},
        'room':     {'room_id'},
    }

    def __init__(self, anchor_name: str, live_id: str, config: dict):
        self.live_id = live_id
        self._anchor_name = anchor_name
        output_cfg = config.get('output', {})
        fmt_cfg = config.get('barrage', {})
        self._fmts = set()
        if fmt_cfg.get('csv', False):
            self._fmts.add('csv')
        if fmt_cfg.get('sqlite', False):
            self._fmts.add('sqlite')
        self._enable_outputs = output_cfg
        self._base_dir = config.get('output_dir', os.path.join(SCRIPT_DIR, 'data'))
        self._dir = self._base_dir                # CSV 会话目录，open() 中更新
        self._live_dir = None                     # SQLite 房间目录，open() 中设置

        self._csv_bufs = {}
        self._csv_writers = {}
        self._csv_fps = {}
        self._db = None
        self._sqlite_bufs = {}
        self._sqlite_stmts = {}      # {msg_type: sql} prepared statement 缓存
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._flush_thread = None
        self._dropped = 0
        self._opened = False

    @property
    def session_dir(self):
        """当前 CSV 会话目录（open() 后可用）。"""
        return self._dir

    def open(self):
        """初始化记录器，创建输出目录。

        目录结构：data/{主播名}/{yyyy-MM-dd_HH-mm-ss}/
        SQLite 数据库位于主播目录下，跨会话追加。
        """
        if self._opened or not self._fmts:
            return
        self._ts = time.strftime('%Y%m%d_%H%M')

        # 房间级目录：主播名
        self._live_dir = get_anchor_dir(self._base_dir, self._anchor_name, self.live_id)

        # 会话级目录（CSV 和 SQLite 都需要，与录制文件同级）
        self._dir = os.path.join(self._live_dir, self._ts)
        os.makedirs(self._dir, exist_ok=True)

        if 'sqlite' in self._fmts:
            self._open_db()

        self._flush_thread = threading.Thread(target=self._bg_flush_loop, daemon=True, name='recorder-flush')
        self._flush_thread.start()
        self._opened = True
        logger.info(f"[数据] 就绪: {self._anchor_name or self.live_id}, 格式={','.join(sorted(self._fmts))}")

    def _ensure_csv(self, msg_type: str):
        """首次收到某类型数据时创建对应的 CSV 文件并写入表头。"""
        if msg_type in self._csv_fps:
            return
        fields = self.CSV_FIELDS.get(msg_type)
        if not fields:
            return
        path = os.path.join(self._dir, f"{msg_type}.csv")
        fp = open(path, 'w', newline='', encoding='utf-8-sig')
        writer = csv.DictWriter(fp, fieldnames=fields)
        writer.writeheader()
        self._csv_fps[msg_type] = fp
        self._csv_writers[msg_type] = writer
        self._csv_bufs[msg_type] = deque(maxlen=100_000)

    # ── SQLite ────────────────────────────────────

    def _open_db(self):
        """打开房间级 SQLite 数据库，建表。

        WAL 模式允许读写并发，synchronous=NORMAL 在 WAL 下仍保证数据安全。
        cache_size=-8000 给予 8MB 页缓存，减少磁盘 I/O。
        """
        db_path = os.path.join(self._dir, 'data.db')
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute('PRAGMA synchronous=NORMAL')
        self._db.execute('PRAGMA cache_size=-8000')
        self._db.execute('PRAGMA temp_store=MEMORY')
        self._init_schema()

    def _init_schema(self):
        """建表 + 索引。13 张弹幕表，全部幂等。

        time 字段存储 Unix 秒级时间戳（INTEGER），用于视频+弹幕时间同步。
        """
        for msg_type, fields in self.CSV_FIELDS.items():
            int_fields = self.INTEGER_FIELDS.get(msg_type, set())
            col_defs = []
            for f in fields:
                # time 列存 Unix 时间戳（INTEGER），其余按 INTEGER_FIELDS 判定
                col_type = 'INTEGER' if (f in int_fields or f == 'time') else 'TEXT'
                col_defs.append(f'"{f}" {col_type}')
            all_cols = 'id INTEGER PRIMARY KEY AUTOINCREMENT, ' + ', '.join(col_defs)

            table = f'"{msg_type}"' if msg_type == 'like' else msg_type
            self._db.execute(f'CREATE TABLE IF NOT EXISTS {table} ({all_cols})')
            self._db.execute(
                f'CREATE INDEX IF NOT EXISTS idx_{msg_type}_time ON {table}("time")'
            )
        self._db.commit()

    def _convert_sqlite_row(self, msg_type, data):
        """将一条数据字典转为 SQLite 行元组，处理类型转换。

        INTEGER 字段：str/int → int，空字符串 / None / 0 → None（NULL）。
        TEXT 字段：任何值 → str。
        time 字段已在 record() 中转为 ISO 8601，此处直接使用。
        """
        fields = self.CSV_FIELDS[msg_type]
        int_fields = self.INTEGER_FIELDS.get(msg_type, set())
        row = []
        for f in fields:
            val = data.get(f, '')
            if f in int_fields:
                if isinstance(val, int):
                    row.append(val)
                elif val is None or val == '':
                    row.append(None)
                else:
                    try:
                        row.append(int(val))
                    except (ValueError, TypeError):
                        row.append(None)
            else:
                # Unix 时间戳（int）原样保留，其余转 str
                row.append(val if isinstance(val, int) else (str(val) if val is not None else ''))
        return tuple(row)

    def _flush_sqlite(self):
        """批量写入 SQLite，每批最多 5000 条，失败时回退 deque。"""
        with self._lock:
            bufs = self._drain_bufs(self._sqlite_bufs)
        for msg_type, batch in bufs.items():
            # 懒缓存 prepared statement
            if msg_type not in self._sqlite_stmts:
                fields = self.CSV_FIELDS.get(msg_type, [])
                table = f'"{msg_type}"' if msg_type == 'like' else msg_type
                placeholders = ', '.join(['?'] * len(fields))
                sql = f'INSERT INTO {table} ({", ".join(fields)}) VALUES ({placeholders})'
                self._sqlite_stmts[msg_type] = sql

            sql = self._sqlite_stmts[msg_type]
            rows = [self._convert_sqlite_row(msg_type, d) for d in batch]
            try:
                self._db.executemany(sql, rows)
                self._db.commit()
            except Exception as e:
                logger.warning(f"[数据] SQLite 写入异常({msg_type} {len(batch)}条): {e}")
                with self._lock:
                    buf = self._sqlite_bufs.get(msg_type)
                    if buf is not None:
                        # 只回退能放下的部分，避免挤出好数据
                        space = buf.maxlen - len(buf)
                        for d in reversed(batch[:space]):
                            buf.appendleft(d)

    def record(self, msg_type: str, data: dict):
        if not self._opened:
            return
        with self._lock:
            if not self._enable_outputs.get(msg_type, True):
                return
            if 'csv' in self._fmts:
                if msg_type not in self._csv_bufs:
                    self._ensure_csv(msg_type)
                buf = self._csv_bufs.get(msg_type)
                if buf is not None:
                    if len(buf) >= buf.maxlen:
                        self._dropped += 1
                    buf.append(data)
            if 'sqlite' in self._fmts:
                buf = self._sqlite_bufs.setdefault(msg_type, deque(maxlen=100_000))
                if len(buf) >= buf.maxlen:
                    self._dropped += 1
                sqlite_data = dict(data)
                sqlite_data['time'] = int(time.time())
                buf.append(sqlite_data)

    def _bg_flush_loop(self):
        """后台刷新循环，每 10s 将缓冲区数据写入磁盘。"""
        consecutive_errors = 0
        while not self._stop.is_set():
            try:
                self._stop.wait(timeout=10.0)
                if self._stop.is_set():
                    break
                self._do_flush()
                consecutive_errors = 0
                if self._dropped > 0:
                    logger.warning(f"[数据] ⚠️ 缓冲区溢出，已丢弃 {self._dropped} 条")
                    self._dropped = 0
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    logger.error(f"[数据] 刷新线程连续 {consecutive_errors} 次异常，继续尝试: {e}")
                else:
                    logger.warning(f"[数据] 刷新异常 ({consecutive_errors}/5)，重试: {e}")
                time.sleep(2)
        # 最终刷新
        try:
            self._do_flush()
        except Exception:
            pass

    @staticmethod
    def _drain_bufs(bufs, limit=5000):
        """从多个 deque 中批量取出数据，每个最多 limit 条。"""
        batches = {}
        for msg_type, buf in bufs.items():
            batch = []
            while buf and len(batch) < limit:
                try:
                    batch.append(buf.popleft())
                except IndexError:
                    break
            if batch:
                batches[msg_type] = batch
        return batches

    def _do_flush(self):
        """执行一次批量刷新：CSV、SQLite 各取最多 5000 条写出。"""
        if 'sqlite' in self._fmts:
            self._flush_sqlite()

        with self._lock:
            csv_batches = self._drain_bufs(self._csv_bufs)

        for msg_type, batch in csv_batches.items():
            writer = self._csv_writers.get(msg_type)
            fp = self._csv_fps.get(msg_type)
            if not writer or not fp:
                continue
            failed_idx = len(batch)
            for i, row in enumerate(batch):
                try:
                    writer.writerow(row)
                except Exception:
                    failed_idx = i
                    break
            fp.flush()
            # 写入失败时，把未写入的数据放回 deque 头部（加锁保护）
            if failed_idx < len(batch):
                with self._lock:
                    buf = self._csv_bufs.get(msg_type)
                    if buf is not None:
                        space = buf.maxlen - len(buf)
                        remaining = batch[failed_idx:]
                        for row in reversed(remaining[:space]):
                            buf.appendleft(row)
                logger.warning(f"[数据] CSV 写入异常，{len(batch) - failed_idx} 条数据已回退")

    def close(self):
        """停止后台线程，刷新剩余数据，关闭所有句柄。"""
        if not self._opened:
            return
        self._stop.set()
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=5)
            if self._flush_thread.is_alive():
                # 线程未退出，尝试同步刷新剩余数据
                try:
                    self._do_flush()
                except Exception:
                    pass
                pending = sum(len(b) for b in self._sqlite_bufs.values()) + sum(len(b) for b in self._csv_bufs.values())
                if pending > 0:
                    logger.warning(f"[数据] 刷新线程未在 5 秒内退出，{pending} 条数据可能未写入")
        for fp in self._csv_fps.values():
            try:
                fp.close()
            except Exception:
                pass
        if self._db is not None:
            try:
                self._db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                self._db.close()
            except Exception:
                pass
            self._db = None
        self._opened = False
        logger.info("[数据] 记录器已关闭")
