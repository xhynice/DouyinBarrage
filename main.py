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

if __package__ is None:
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    if _script_dir not in sys.path:
        sys.path.insert(0, _script_dir)

from service.fetcher import DouyinBarrage
from base.utils import update_room_name_in_config
from base.output import RoomLogFilter

room = None
instances = []
_shutting_down = False
_active_rooms = {}  # {room_id: {'instance': DouyinBarrage, 'thread': Thread, 'config': dict}}
_active_rooms_lock = threading.Lock()

logger = logging.getLogger(__name__)


def signal_handler(signum, frame):
    """信号处理函数，优雅退出（单/多房间通用）"""
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    print("\n【收到停止信号，正在优雅退出...】")
    if instances:
        for r in instances:
            try:
                r.stop()
            except Exception:
                pass
    elif room:
        room.stop()


def show_usage():
    print("""
抖音直播间弹幕数据采集器

用法: python main.py [直播间ID] [选项]

选项:
  --log-level <级别>        覆盖日志级别 (DEBUG/INFO/WARNING/ERROR/NONE)
  --live-end <行为>         直播结束行为: wait=等待重开播, stop=结束退出
""")


def validate_live_id(live_id: str) -> bool:
    """验证直播间 ID 格式。
    
    抖音直播间 ID 为 6-18 位纯数字。
    
    Args:
        live_id: 直播间 ID 字符串。
        
    Returns:
        True 表示格式有效，False 表示无效。
    """
    return bool(re.match(r'^\d{6,18}$', live_id))


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

                if not rid or not validate_live_id(rid) or rid in seen:
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


def run_room(room_cfg, log_level, live_stop):
    """单个房间的采集线程。

    Args:
        room_cfg: {'id': str, 'name': str} 配置。
        log_level: 日志级别。
        live_stop: 直播结束后是否停止退出 (bool)。
    """
    live_id = room_cfg['id']

    try:
        instance = DouyinBarrage(live_id, log_level=log_level, on_room_info=_make_on_room_info(room_cfg), multi_room=True)
        with _active_rooms_lock:
            _active_rooms[live_id] = {'instance': instance, 'thread': threading.current_thread(), 'config': room_cfg}

        if live_stop is not None:
            instance.config['live_stop'] = live_stop

        instance.start()
    except Exception as e:
        logger.error(f"[{live_id}] 采集异常: {e}")
    finally:
        with _active_rooms_lock:
            _active_rooms.pop(live_id, None)



def _start_room(room_cfg, log_level, live_stop):
    """启动单个房间线程（热加载用）。"""
    t = threading.Thread(
        target=run_room,
        args=(room_cfg, log_level, live_stop),
        name=f"room-{room_cfg['id']}",
        daemon=False,
    )
    t.start()
    return t


class RoomsWatcher:
    """监控 rooms.txt 变化，自动增删房间（热加载）。"""

    def __init__(self, rooms_file, log_level, live_stop, check_interval=10):
        self._rooms_file = rooms_file
        self._log_level = log_level
        self._live_stop = live_stop
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

        if not to_remove and not to_add:
            logger.info("[热加载] rooms.txt 变化但房间列表无变化")
            return

        if to_remove:
            logger.info(f"[热加载] 移除房间: {to_remove}")
            with _active_rooms_lock:
                for rid in to_remove:
                    entry = _active_rooms.get(rid)
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
                _start_room(r, self._log_level, self._live_stop)
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

    # 纯直播间ID（6-18位数字）
    if validate_live_id(user_input):
        return ('single', user_input, [])

    # 统一分隔符：逗号和空格都作为分隔符
    raw_parts = re.split(r'[\s,]+', user_input)
    parts = [p.strip() for p in raw_parts if p.strip()]

    selected_rooms = []
    seen = set()

    for part in parts:
        # 尝试解析范围 1-3
        range_idxs = _parse_range(part, len(rooms))
        if range_idxs:
            for idx in range_idxs:
                if idx not in seen:
                    seen.add(idx)
                    selected_rooms.append(rooms[idx])
            continue

        # 尝试解析单个编号
        try:
            idx = int(part) - 1
            if 0 <= idx < len(rooms):
                if idx not in seen:
                    seen.add(idx)
                    selected_rooms.append(rooms[idx])
            else:
                warnings.append(f"编号 {part} 超出范围（1-{len(rooms)}），已跳过")
        except ValueError:
            # 尝试作为直播间ID解析
            if validate_live_id(part):
                if part not in seen:
                    seen.add(part)
                    selected_rooms.append({'id': part, 'name': ''})
            else:
                warnings.append(f"'{part}' 不是有效的编号或直播间ID，已跳过")

    if selected_rooms:
        return ('multi', selected_rooms, warnings)

    return None, None, warnings


def main_multi(room_list, log_level, live_stop):
    """多房间模式入口（支持热加载 rooms.txt）。

    Args:
        room_list: 房间配置列表 [{'id': str, 'name': str}, ...]
        log_level: 日志级别
        live_stop: 直播结束后是否停止退出 (bool)。
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
    print("按 Ctrl+C 停止所有采集")
    print("热加载: 修改 rooms.txt 自动增删房间\n")

    # 逐个启动，错开签名调用
    for i, room_cfg in enumerate(room_list):
        if room_cfg.get('name'):
            RoomLogFilter.update_anchor(room_cfg['id'], room_cfg['name'])
        _start_room(room_cfg, log_level, live_stop)
        if i < len(room_list) - 1:
            time.sleep(3.5)

    # 启动文件监控
    rooms_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rooms.txt')
    watcher = RoomsWatcher(rooms_file, log_level, live_stop)
    watcher.start()

    print(f"\n[主控] {len(room_list)} 个采集线程已启动\n")

    # 等待：直到所有房间线程退出或用户中断
    try:
        while not _shutting_down:
            time.sleep(1)
            with _active_rooms_lock:
                if not _active_rooms:
                    break
    except KeyboardInterrupt:
        print("\n【用户中断，停止所有采集】")
        _shutting_down = True
        watcher.stop()
        with _active_rooms_lock:
            for entry in list(_active_rooms.values()):
                try:
                    entry['instance'].stop()
                except Exception:
                    pass
        time.sleep(2)

    watcher.stop()
    print("[主控] 所有采集已停止")



def start_single_room(live_id, log_level, live_stop, rooms=None):
    """启动单房间采集。

    Args:
        live_id: 直播间ID
        log_level: 日志级别
        live_stop: 直播结束后是否停止退出 (bool)。
        rooms: 已加载的房间列表（避免重复读取配置），为 None 时自动加载
    """
    
    global room

    print("=" * 45)
    print("抖音直播间弹幕数据采集器")
    print("=" * 45)
    print(f"直播间 ID: {live_id}")
    if log_level:
        print(f"日志级别: {log_level}")
    if live_stop is not None:
        print(f"直播结束行为: {'结束退出' if live_stop else '等待重开播'}")
    print("=" * 45)
    print("按 Ctrl+C 停止采集\n")

    # 从配置中查找该房间，用于判断 name 是否已存在
    if rooms is None:
        rooms = load_rooms_from_config()
    room_cfg = next((r for r in rooms if r['id'] == live_id), {'id': live_id, 'name': ''})

    room = DouyinBarrage(live_id, log_level=log_level, on_room_info=_make_on_room_info(room_cfg))
    instances.append(room)

    if live_stop is not None:
        room.config['live_stop'] = live_stop

    try:
        room.start()
    except KeyboardInterrupt:
        print("\n【用户中断，停止采集】")
        room.stop()
    except Exception as e:
        print(f"\n【采集失败: {e}】")
        room.stop()
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description='抖音直播间弹幕数据采集器',
        add_help=False,
    )
    parser.add_argument('live_id', nargs='?', help='直播间 ID（不提供则交互式选择）')
    parser.add_argument('--log-level', choices=['DEBUG', 'BARRAGE', 'INFO', 'WARNING', 'ERROR', 'NONE'],
                        help='覆盖日志级别')
    parser.add_argument('--live-stop', action='store_true',
                        help='直播结束后停止退出（默认跟随配置文件）')
    parser.add_argument('--live-wait', action='store_true',
                        help='直播结束后等待重开播（默认跟随配置文件）')
    parser.add_argument('--all', action='store_true',
                        help='采集 rooms.txt 中全部未注释的房间（跳过交互选择）')

    args = parser.parse_args()

    # 处理互斥的 live_stop / live_wait 参数
    if args.live_stop and args.live_wait:
        print("错误：--live-stop 和 --live-wait 不能同时使用")
        sys.exit(1)
    live_stop = True if args.live_stop else (False if args.live_wait else None)

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
        if len(rooms) == 1:
            start_single_room(rooms[0]['id'], args.log_level, live_stop, rooms=rooms)
        else:
            main_multi(rooms, args.log_level, live_stop)
        return

    # 命令行直接指定了ID，单房间模式
    if args.live_id:
        if not validate_live_id(args.live_id):
            print(f"错误：无效的直播间 ID '{args.live_id}'（必须是 6-18 位纯数字）")
            sys.exit(1)
        start_single_room(args.live_id, args.log_level, live_stop)
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
                if not validate_live_id(live_id):
                    print("错误：直播间 ID 必须是 6-18 位纯数字")
                    continue
                break
            start_single_room(live_id, args.log_level, live_stop, rooms=rooms)
            return

        def show_room_list():
            print("                 _.")
            print("               <(o  )  _,,,°")
            print("---------------(__''___) ---------------")
            print("   弹幕采集器 04.27.2026 by NcieXHY'")
            print("-----------------------------------------")


            # print("=" * 45)
            # print("抖音直播间弹幕数据采集器")
            # print("=" * 45)
            #print(f"已从配置加载 {len(rooms)} 个房间：\n")
            for i, r in enumerate(rooms, 1):
                label = f"{r['id']}"
                if r['name']:
                    label += f" - {r['name']}"
                print(f"  [{i}] {label}")


        def show_input_help():
            print("""
  编号        1 或 1 2 3 或 1,2,3 或 1-3
  直播间ID    536863152858
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
                start_single_room(data, args.log_level, live_stop, rooms=rooms)
                break
            elif mode == 'multi':
                if len(data) == 1:
                    start_single_room(data[0]['id'], args.log_level, live_stop, rooms=rooms)
                else:
                    main_multi(data, args.log_level, live_stop)
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
