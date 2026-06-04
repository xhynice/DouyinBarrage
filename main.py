#!/usr/bin/python
# coding:utf-8
"""抖音直播间弹幕数据采集器 - 启动入口

用法:
  python main.py                          # 交互式选择房间
  python main.py 536863152858             # 直接指定直播间ID
  python main.py 536863152858 --live-end stop  # 直播结束后退出
"""

import argparse
import logging
import os
import re
import signal
import sys
import threading
import time

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

if __package__ is None:
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    if _script_dir not in sys.path:
        sys.path.insert(0, _script_dir)

from service.fetcher import DouyinBarrage
from base.utils import update_room_name_in_config, load_config, DEFAULT_CONFIG
from base.output import RoomLogFilter, get_room_statuses
from service.recorder import check_ffmpeg

_shutting_down = False
_active_rooms = {}  # {room_id: {'instance': DouyinBarrage, 'thread': Thread, 'config': dict}}
_active_rooms_lock = threading.Lock()
_rooms_file_lock = threading.Lock()  # 保护 rooms.txt 读写

logger = logging.getLogger(__name__)

# ── 全局显示配置 ──
_display_config = {
    'log_level': 'INFO',
    'record_enabled': False,
    'record_quality': '原画',
    'record_format': 'ts',
    'barrage_cfg': {'csv': True, 'sqlite': False},
}


def status_line():
    """构建单行状态，供 display_loop 周期性输出。"""
    statuses = get_room_statuses()
    if not statuses:
        return ''
    with _active_rooms_lock:
        active_count = len(_active_rooms)
    now_str = time.strftime('%H:%M:%S')
    rec_enabled = _display_config.get('record_enabled', False)
    rec_quality = _display_config.get('record_quality', '原画')
    rec_fmt = _display_config.get('record_format', 'ts')

    # 弹幕保存格式
    barrage_cfg = _display_config.get('barrage_cfg', {})
    fmts = []
    if barrage_cfg.get('csv', True):
        fmts.append('csv')
    if barrage_cfg.get('sqlite', False):
        fmts.append('sqlite')
    barrage_fmt = ','.join(fmts) if fmts else '无'

    parts = [f"共监测{active_count}个直播中"]
    if rec_enabled:
        parts.append(f"录制: [{rec_quality}] {rec_fmt}")
    parts.append(f"弹幕: {barrage_fmt}")
    parts.append(f"当前时间: {now_str}")

    for live_id, info in statuses.items():
        if info.get('status') == 'waiting':
            anchor = info.get('anchor', live_id)
            interval = info.get('interval', '?')
            parts.append(f"{anchor} [监测中] ({interval}s)")
        elif info.get('status') == 'collecting':
            anchor = info.get('anchor', live_id)
            count = info.get('msg_count', 0)
            elapsed = info.get('elapsed', 0)
            rate = count / elapsed if elapsed > 0 else 0
            s = f"{anchor} [弹幕] {count}条 ({rate:.1f}/s)"
            rec_elapsed = info.get('rec_elapsed', 0)
            if rec_elapsed > 0:
                m, sec = divmod(int(rec_elapsed), 60)
                h, m = divmod(m, 60)
                dur = f"{h:02d}:{m:02d}:{sec:02d}" if h > 0 else f"{m:02d}:{sec:02d}"
                s += f" [录制] {dur}"
            parts.append(s)

    return " | ".join(parts)


def signal_handler(signum, frame):
    """信号处理函数，仅设退出标志，清理由主循环处理（避免在信号上下文中获取锁）。"""
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    print("\n【收到停止信号，正在优雅退出...】")


def show_usage():
    print("""
抖音直播间弹幕数据采集器

用法: python main.py [直播间ID] [选项]

选项:
  --log-level <级别>        覆盖日志级别 (DEBUG/INFO/WARNING/ERROR/NONE)
  --live-stop               直播结束后停止退出
  --live-wait               直播结束后等待重开播
  --record                 启用直播流录制
  --all                     采集 rooms.txt 中全部未注释的房间
""")


def load_rooms_from_config(rooms_file='rooms.txt'):
    """从 rooms.txt 读取房间列表。

    文件格式：
        - 每行一个房间：id,name（逗号分隔）
        - # 开头表示不启用
        - 空行自动跳过

    Returns:
        [{'id': str, 'name': str}, ...] 列表，无配置时返回空列表。
    """
    if not os.path.isabs(rooms_file):
        rooms_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), rooms_file)

    if not os.path.exists(rooms_file):
        return []

    result = []
    seen = set()

    try:
        with open(rooms_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                enabled = not line.startswith('#')
                if not enabled:
                    line = line[1:].strip()

                if not line:
                    continue

                parts = line.split(',', 1)
                rid = parts[0].strip()
                name = parts[1].strip() if len(parts) > 1 else ''

                if not rid or rid in seen:
                    logger.warning(f"[配置] 跳过无效房间 ID: {rid}")
                    continue
                seen.add(rid)

                if enabled:
                    result.append({
                        'id': rid,
                        'name': name,
                    })
    except Exception as e:
        logger.error(f"[配置] 读取房间文件失败: {e}")
        return []

    return result


def _make_on_room_info(room_cfg):
    """创建 on_room_info 回调：仅在配置 name 为空时自动补全主播名。"""
    def on_room_info(rid, anchor_name):
        if not anchor_name:
            return
        if not room_cfg.get('name'):
            room_cfg['name'] = anchor_name
            update_room_name_in_config(rid, anchor_name)
            logger.info(f"[配置] 已自动更新主播名：{rid} → {anchor_name}")
    return on_room_info


def run_room(room_cfg, log_level, live_stop, record=None):
    """单个房间的采集线程。

    Args:
        room_cfg: {'id': str, 'name': str} 配置。
        log_level: 日志级别。
        live_stop: 直播结束后是否停止退出 (bool)。
        record: 是否启用录制 (bool)。
    """
    live_id = room_cfg['id']
    instance = None

    try:
        instance = DouyinBarrage(live_id, log_level=log_level, on_room_info=_make_on_room_info(room_cfg))
        with _active_rooms_lock:
            _active_rooms[live_id] = {'instance': instance, 'thread': threading.current_thread(), 'config': room_cfg}

        if live_stop is not None:
            instance.config['live_stop'] = live_stop
        if record is not None:
            instance.config.setdefault('record', {})['enabled'] = record

        instance.start()

        # 检查是否因房间不存在而退出，自动注释 rooms.txt
        if instance.stop_reason == 'room_not_found':
            _comment_room_in_rooms_file(live_id)

    except Exception as e:
        logger.error(f"[{live_id}] 采集异常: {e}")
    finally:
        if instance:
            try:
                instance.stop()
            except Exception:
                pass
        with _active_rooms_lock:
            _active_rooms.pop(live_id, None)


def _comment_room_in_rooms_file(room_id, rooms_file=None):
    """在 rooms.txt 中注释掉指定房间，防止热加载重复启动。"""
    if rooms_file is None:
        rooms_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rooms.txt')
    with _rooms_file_lock:
        try:
            with open(rooms_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            changed = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped and not stripped.startswith('#'):
                    # 匹配行首的 room_id（可能带主播名，如 "12345,主播名"）
                    rid = stripped.split(',')[0].strip()
                    if rid == room_id:
                        lines[i] = '# ' + line
                        changed = True
            if changed:
                with open(rooms_file, 'w', encoding='utf-8') as f:
                    f.writelines(lines)
                logger.info(f"[热加载] 已自动注释无效房间 {room_id}（写入 rooms.txt）")
        except Exception as e:
            logger.error(f"[热加载] 注释房间 {room_id} 失败: {e}")


def _start_room(room_cfg, log_level, live_stop, record=None):
    """启动单个房间线程（热加载用）。"""
    t = threading.Thread(
        target=run_room,
        args=(room_cfg, log_level, live_stop, record),
        name=f"room-{room_cfg['id']}",
        daemon=False,
    )
    t.start()
    return t


class RoomsWatcher:
    """监控 rooms.txt 变化，自动增删房间（热加载）。"""

    def __init__(self, rooms_file, log_level, live_stop, record=None, check_interval=10):
        self._rooms_file = rooms_file
        self._log_level = log_level
        self._live_stop = live_stop
        self._record = record
        self._interval = check_interval
        self._last_mtime = self._get_mtime()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._watch_loop, name="rooms-watcher", daemon=True)

    def _get_mtime(self):
        try:
            return os.path.getmtime(self._rooms_file)
        except OSError:
            return 0

    def start(self):
        self._thread.start()
        logger.info(f"[热加载] 监控 {self._rooms_file} (间隔 {self._interval}s)")

    def stop(self):
        self._stop_event.set()

    def _watch_loop(self):
        while not self._stop_event.is_set():
            self._stop_event.wait(self._interval)
            if self._stop_event.is_set():
                break
            current_mtime = self._get_mtime()
            if current_mtime == self._last_mtime:
                continue
            self._last_mtime = current_mtime
            self._reload_rooms()

    def _reload_rooms(self):
        new_rooms = load_rooms_from_config(self._rooms_file)
        new_ids = {r['id'] for r in new_rooms}

        with _active_rooms_lock:
            current_ids = set(_active_rooms.keys())
            to_remove = current_ids - new_ids
            to_add = new_ids - current_ids
            # 提前获取要移除的房间条目，避免再次获取锁
            entries = [(rid, _active_rooms.get(rid)) for rid in to_remove]

        if not to_remove and not to_add:
            logger.debug("[热加载] rooms.txt 变化但房间列表无变化")
            return

        if to_remove:
            logger.info(f"[热加载] 移除房间: {to_remove}")
            for rid, entry in entries:
                if entry:
                    try:
                        entry['instance'].stop()
                    except Exception as e:
                        logger.error(f"[热加载] 停止 {rid} 异常: {e}")

        if to_add:
            new_room_list = [r for r in new_rooms if r['id'] in to_add]
            logger.info(f"[热加载] 新增房间: {[r['id'] for r in new_room_list]}")
            for r in new_room_list:
                if r.get('name'):
                    RoomLogFilter.update_anchor(r['id'], r['name'])
                _start_room(r, self._log_level, self._live_stop, record=self._record)
                time.sleep(3.5)  # 错开启动

        logger.info(f"[热加载] 完成，当前 {len(new_ids)} 个房间")


def _parse_range(part, rooms_count):
    """解析范围输入如 '1-3' 或 '2-5'。

    Returns:
        list[int]: 有效的 0-based 索引列表，无效范围返回空列表。
    """
    if '-' not in part:
        return []
    ends = part.split('-', 1)
    if len(ends) != 2:
        return []
    try:
        start = int(ends[0].strip())
        end = int(ends[1].strip())
    except ValueError:
        return []
    if start > end:
        start, end = end, start
    result = []
    for idx in range(start - 1, end):
        if 0 <= idx < rooms_count:
            result.append(idx)
    return result


def parse_user_input(user_input, rooms):
    """解析用户输入，返回采集模式和相关参数。

    支持的输入格式：
        - 单编号: 1
        - 多编号逗号分隔: 1,2,3
        - 多编号空格分隔: 1 2 3
        - 范围选择: 1-3
        - 混合: 1,3-5,7
        - 直播间ID: 536863152858
        - 特殊指令:
            'a' / 'all'  — 选择全部房间
            'q' / 'quit' — 退出程序
            '?' / 'h'    — 显示帮助

    Args:
        user_input: 用户输入的字符串
        rooms: 配置的房间列表

    Returns:
        tuple: (mode, data, warnings)
            - ('single', live_id, []): 单房间模式
            - ('multi', room_list, warnings): 多房间模式
            - ('quit', None, []): 用户要求退出
            - ('help', None, []): 用户请求帮助
            - (None, None, []): 空输入，需重新输入
    """
    warnings = []

    if not user_input:
        return None, None, []

    user_input = user_input.strip()

    # 安全过滤：限制长度，防止异常输入
    if len(user_input) > 200:
        warnings.append("输入过长，已截断处理")
        user_input = user_input[:200]

    # 过滤控制字符（保留数字、字母、逗号、空格、连字符）
    cleaned = re.sub(r'[^\w\s,\-]', '', user_input)
    if cleaned != user_input:
        warnings.append("已过滤输入中的非法字符")
        user_input = cleaned.strip()
        if not user_input:
            return None, None, warnings

    lower = user_input.lower()

    # 特殊指令处理
    if lower in ('q', 'quit', 'exit'):
        return ('quit', None, [])
    if lower in ('?', 'h', 'help'):
        return ('help', None, [])
    if lower in ('a', 'all'):
        if rooms:
            return ('multi', rooms[:], [])
        return None, None, warnings

    # 纯数字输入：0=全部房间，1~N=编号，超出范围=直播间ID
    if user_input.isdigit():
        num = int(user_input)
        if num == 0:
            if rooms:
                return ('multi', rooms[:], [])
            return None, None, warnings
        idx = num - 1
        if 0 <= idx < len(rooms):
            return ('multi', [rooms[idx]], [])
        return ('single', user_input, [])

    # 统一分隔符：逗号和空格都作为分隔符
    raw_parts = re.split(r'[\s,]+', user_input)
    parts = [p.strip() for p in raw_parts if p.strip()]

    selected_rooms = []
    seen_idxs = set()   # 跟踪已选的房间列表索引
    seen_ids = set()    # 跟踪已选的直播间 ID

    for part in parts:
        # 尝试解析范围 1-3
        range_idxs = _parse_range(part, len(rooms))
        if range_idxs:
            for idx in range_idxs:
                if idx not in seen_idxs:
                    seen_idxs.add(idx)
                    selected_rooms.append(rooms[idx])
            continue

        # 尝试解析单个编号
        try:
            idx = int(part) - 1
            if 0 <= idx < len(rooms):
                if idx not in seen_idxs:
                    seen_idxs.add(idx)
                    selected_rooms.append(rooms[idx])
            else:
                warnings.append(f"编号 {part} 超出范围（1-{len(rooms)}），已跳过")
        except ValueError:
            # 尝试作为直播间ID解析
            if part.isdigit():
                if part not in seen_ids:
                    seen_ids.add(part)
                    selected_rooms.append({'id': part, 'name': ''})
            else:
                warnings.append(f"'{part}' 不是有效的编号或直播间ID，已跳过")

    if selected_rooms:
        return ('multi', selected_rooms, warnings)

    return None, None, warnings


def main_multi(room_list, log_level, live_stop, record=None):
    """多房间模式入口（支持热加载 rooms.txt）。

    Args:
        room_list: 房间配置列表 [{'id': str, 'name': str}, ...]
        log_level: 日志级别
        live_stop: 直播结束后是否停止退出 (bool)。
        record: 是否启用录制 (bool)。
    """
    global _shutting_down
    if not room_list:
        print("错误：未选择任何房间")
        sys.exit(1)

    print("")
    print("=" * 45)
    print("开始多房间采集（热加载模式）")
    print("=" * 45)
    print("")
    print(f"房间数量: {len(room_list)}")
    for r in room_list:
        label = f"{r['id']}"
        if r['name']:
            label += f" ({r['name']})"
        else:
            label += " (主播名待获取)"
        print(f"  - {label}")
    if log_level:
        print(f"日志级别: {log_level}")
    if live_stop is not None:
        print(f"直播结束行为: {'结束退出' if live_stop else '等待重开播'}")
    # 更新全局显示配置
    cfg = load_config('config.yaml', DEFAULT_CONFIG)
    rc = cfg.get('record', {})
    _display_config['record_enabled'] = rc.get('enabled', False) if record is None else record
    if _display_config['record_enabled']:
        print("录制: 已启用")
    print("按 Ctrl+C 停止所有采集")
    print("热加载: 修改 rooms.txt 自动增删房间\n")
    _display_config['record_quality'] = rc.get('quality', '原画')
    _display_config['record_format'] = rc.get('format', 'ts')
    _display_config['barrage_cfg'] = cfg.get('barrage', {'csv': True, 'sqlite': False})
    if log_level:
        _display_config['log_level'] = log_level

    # 逐个启动，错开签名调用
    for i, room_cfg in enumerate(room_list):
        if room_cfg.get('name'):
            RoomLogFilter.update_anchor(room_cfg['id'], room_cfg['name'])
        _start_room(room_cfg, log_level, live_stop, record=record)
        if i < len(room_list) - 1:
            time.sleep(3.5)

    # 周期性状态行（房间启动后再开始，确保日志已刷出）
    def _periodic_status():
        while not _shutting_down:
            time.sleep(5)
            line = status_line()
            if line:
                print(f"\n{line}")
    threading.Thread(target=_periodic_status, daemon=True).start()

    # 启动文件监控
    rooms_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rooms.txt')
    watcher = RoomsWatcher(rooms_file, log_level, live_stop, record=record)
    watcher.start()

    # 启动 API 服务器（如果启用）
    api_cfg = cfg.get('api', {})
    if api_cfg.get('enabled', False):
        from service.api import start_api_server
        start_api_server(
            api_cfg.get('host', '0.0.0.0'),
            api_cfg.get('port', 8088),
            _active_rooms,
            _active_rooms_lock,
            load_rooms_from_config,
        )

    print(f"\n[主控] {len(room_list)} 个采集线程已启动\n")

    # 等待直到所有房间线程退出或收到停止信号
    while not _shutting_down:
        time.sleep(1)
        with _active_rooms_lock:
            if not _active_rooms and not _shutting_down:
                break

    # 统一清理（signal_handler 只设标志，实际操作在此处执行）
    print("\n【停止所有采集】")
    watcher.stop()
    with _active_rooms_lock:
        entries = list(_active_rooms.values())
    for entry in entries:
        try:
            entry['instance'].stop()
        except Exception:
            pass
    time.sleep(2)
    print("[主控] 所有采集已停止")




def main():
    parser = argparse.ArgumentParser(
        description='抖音直播间弹幕数据采集器',
        add_help=False,
    )
    parser.add_argument('live_id', nargs='?', help='直播间 ID（不提供则交互式选择）')
    parser.add_argument('--log-level', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'NONE'],
                        help='覆盖日志级别')
    parser.add_argument('--live-stop', action='store_true',
                        help='直播结束后停止退出（默认跟随配置文件）')
    parser.add_argument('--live-wait', action='store_true',
                        help='直播结束后等待重开播（默认跟随配置文件）')
    parser.add_argument('--record', action='store_true',
                        help='启用直播流录制（覆盖配置文件中的 record.enabled）')
    parser.add_argument('--all', action='store_true',
                        help='采集 rooms.txt 中全部未注释的房间（跳过交互选择）')

    args = parser.parse_args()

    # 处理互斥的 live_stop / live_wait 参数
    if args.live_stop and args.live_wait:
        print("错误：--live-stop 和 --live-wait 不能同时使用")
        sys.exit(1)
    live_stop = True if args.live_stop else (False if args.live_wait else None)

    record = True if args.record else None

    # 检查 ffmpeg（启用录制时）
    if record:
        if not check_ffmpeg():
            print("错误：启用录制但未找到 ffmpeg，请先安装 FFmpeg")
            sys.exit(1)

    # 切换到脚本所在目录（配置文件、cookie.txt 相对于此目录）
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # 注册信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # --all: 采集 rooms.txt 全部未注释房间，跳过交互
    if args.all:
        rooms = load_rooms_from_config()
        if not rooms:
            print("错误：rooms.txt 中无可用房间")
            sys.exit(1)
        for r in rooms:
            if r.get('name'):
                RoomLogFilter.update_anchor(r['id'], r['name'])
        main_multi(rooms, args.log_level, live_stop, record=record)
        return

    # 命令行直接指定了ID，走统一的多房间入口（单房间也热加载）
    if args.live_id:
        rooms = load_rooms_from_config()
        room_cfg = next((r for r in rooms if r['id'] == args.live_id), {'id': args.live_id, 'name': ''})
        if room_cfg.get('name'):
            RoomLogFilter.update_anchor(room_cfg['id'], room_cfg['name'])
        main_multi([room_cfg], args.log_level, live_stop, record=record)
    else:
        # 交互式选择
        rooms = load_rooms_from_config()

        # 从配置文件预填充主播名映射（日志前缀从第一行就能显示主播名）
        for r in rooms:
            if r.get('name'):
                RoomLogFilter.update_anchor(r['id'], r['name'])

        if not rooms:
            # 无配置，直接手动输入ID
            show_usage()
            while True:
                try:
                    live_id = input("\n请输入直播间 ID: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n已取消")
                    sys.exit(0)
                if not live_id:
                    print("直播间 ID 不能为空")
                    continue
                break
            main_multi([{'id': live_id, 'name': ''}], args.log_level, live_stop, record=record)
            return

        def show_room_list():
            print("                 _.")
            print("               <(o  )  _,,,°")
            print("---------------(__''___) ---------------")
            print("   弹幕采集器 by NcieXHY'")
            print("-----------------------------------------")
            for i, r in enumerate(rooms, 1):
                label = f"{r['id']}"
                if r['name']:
                    label += f" - {r['name']}"
                print(f"  [{i}] {label}")


        def show_input_help():
            print("""
  编号        1 或 1 2 3
  直播间ID    536863152858
  输入 0 采集全部房间
  输入 q 退出程序
""")

        show_room_list()
        show_input_help()

        while True:
            try:
                user_input = input("请选择: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n已取消")
                sys.exit(0)

            result = parse_user_input(user_input, rooms)
            mode, data, warnings = result

            if warnings:
                for warning in warnings:
                    print(f"[提示] {warning}")

            if mode == 'quit':
                print("已退出")
                sys.exit(0)
            elif mode == 'help':
                show_input_help()
                continue
            elif mode == 'single':
                room_cfg = next((r for r in rooms if r['id'] == data), {'id': data, 'name': ''})
                if room_cfg.get('name'):
                    RoomLogFilter.update_anchor(room_cfg['id'], room_cfg['name'])
                main_multi([room_cfg], args.log_level, live_stop, record=record)
                break
            elif mode == 'multi':
                for r in data:
                    if r.get('name'):
                        RoomLogFilter.update_anchor(r['id'], r['name'])
                main_multi(data, args.log_level, live_stop, record=record)
                break
            else:
                # mode is None (空输入或全部无效)
                if not user_input:
                    print("[提示] 输入不能为空，请重新选择")
                else:
                    print("[提示] 未识别到有效选择，请重新输入")
                continue


if __name__ == '__main__':
    main()
