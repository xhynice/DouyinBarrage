"""轻量级 HTTP API 服务器。

基于 Python 内置 http.server，零外部依赖。
提供房间列表、房间详情、系统状态查询接口。

数据来源（全部复用已有结构，无额外采集）：
    - _active_rooms: main.py 中的运行实例字典
    - get_room_statuses(): QueueHandler 中的房间状态
    - load_rooms_from_config(): rooms.txt 全部房间
"""

import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _make_handler(active_rooms_ref, rooms_lock_ref, rooms_loader):
    """创建 API 请求处理类（闭包注入数据引用）。

    Args:
        active_rooms_ref: 对 main._active_rooms 的引用。
        rooms_lock_ref: 对 main._active_rooms_lock 的引用。
        rooms_loader: 加载 rooms.txt 的函数。

    Returns:
        ApiHandler 类。
    """

    class ApiHandler(BaseHTTPRequestHandler):
        """API 请求处理器。"""

        def log_message(self, format, *args):
            """重定向 http.server 日志到项目 logger。"""
            logger.debug(f"[API] {args[0] if args else ''}")

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip('/')

            if path == '/api/rooms':
                self._handle_rooms()
            elif path.startswith('/api/rooms/'):
                room_id = path.split('/api/rooms/', 1)[1]
                self._handle_room_detail(room_id)
            elif path == '/api/status':
                self._handle_status()
            else:
                self._json_response(404, {'error': 'Not found'})

        def _handle_status(self):
            """系统概览：总房间数、直播中、录制中、等待开播。"""
            from base.output import get_room_statuses
            statuses = get_room_statuses()

            total = 0
            live = 0
            recording = 0
            waiting = 0
            disabled = 0

            all_rooms = rooms_loader()
            total = len(all_rooms)

            for room in all_rooms:
                if not room.get('enabled', True):
                    disabled += 1
                    continue
                rid = room['id']
                st = statuses.get(rid, {})
                status = st.get('status', '')
                if status == 'collecting':
                    live += 1
                elif status == 'waiting':
                    waiting += 1

            with rooms_lock_ref:
                for entry in active_rooms_ref.values():
                    inst = entry.get('instance')
                    if inst and inst._video_recorder and inst._video_recorder.is_recording:
                        recording += 1

            self._json_response(200, {
                'total': total,
                'enabled': total - disabled,
                'disabled': disabled,
                'live': live,
                'recording': recording,
                'waiting': waiting,
            })

        def _handle_rooms(self):
            """全部房间列表（含禁用的）。"""
            from base.output import get_room_statuses
            statuses = get_room_statuses()
            all_rooms = rooms_loader()

            result = []
            for room in all_rooms:
                rid = room['id']
                name = room.get('name', '')
                enabled = room.get('enabled', True)

                entry = {'id': rid, 'name': name, 'enabled': enabled}

                if not enabled:
                    entry['live_status'] = 'disabled'
                else:
                    st = statuses.get(rid, {})
                    status = st.get('status', 'offline')
                    entry['live_status'] = status
                    entry['anchor_name'] = st.get('anchor', name)
                    entry['msg_count'] = st.get('msg_count', 0)
                    entry['elapsed'] = st.get('elapsed', '')

                    # 从实例获取更多信息
                    with rooms_lock_ref:
                        room_entry = active_rooms_ref.get(rid)
                    if room_entry:
                        inst = room_entry.get('instance')
                        if inst:
                            info = inst._room_info or {}
                            entry['room_title'] = info.get('room_title', '')
                            entry['room_id'] = info.get('room_id', '')
                            entry['is_recording'] = bool(
                                inst._video_recorder and inst._video_recorder.is_recording)
                            if entry['is_recording']:
                                entry['rec_elapsed'] = st.get('rec_elapsed', '')
                                entry['stream_quality'] = inst._record_cfg.get('quality', '')

                result.append(entry)

            self._json_response(200, result)

        def _handle_room_detail(self, room_id):
            """单个房间详情（含 WS 地址和流地址）。"""
            from base.output import get_room_statuses
            statuses = get_room_statuses()
            all_rooms = rooms_loader()

            # 查找房间配置
            room_cfg = None
            for room in all_rooms:
                if room['id'] == room_id:
                    room_cfg = room
                    break

            if not room_cfg:
                self._json_response(404, {'error': f'Room {room_id} not found'})
                return

            name = room_cfg.get('name', '')
            enabled = room_cfg.get('enabled', True)

            result = {
                'id': room_id,
                'name': name,
                'enabled': enabled,
            }

            if not enabled:
                result['live_status'] = 'disabled'
                self._json_response(200, result)
                return

            st = statuses.get(room_id, {})
            result['live_status'] = st.get('status', 'offline')
            result['anchor_name'] = st.get('anchor', name)
            result['msg_count'] = st.get('msg_count', 0)
            result['elapsed'] = st.get('elapsed', '')

            with rooms_lock_ref:
                room_entry = active_rooms_ref.get(room_id)

            if room_entry:
                inst = room_entry.get('instance')
                if inst:
                    info = inst._room_info or {}
                    result['room_title'] = info.get('room_title', '')
                    result['room_id'] = info.get('room_id', '')
                    result['sec_uid'] = info.get('sec_uid', '')
                    result['ws_url'] = inst._ws_url
                    result['stream_url'] = inst._stream_url
                    result['is_recording'] = bool(
                        inst._video_recorder and inst._video_recorder.is_recording)
                    if result['is_recording']:
                        result['rec_elapsed'] = st.get('rec_elapsed', '')
                        result['stream_quality'] = inst._record_cfg.get('quality', '')
                        result['record_dir'] = getattr(inst._video_recorder, '_session_dir', '')

            self._json_response(200, result)

        def _json_response(self, code, data):
            """发送 JSON 响应。"""
            body = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)

    return ApiHandler


def start_api_server(host, port, active_rooms, active_rooms_lock, rooms_loader):
    """启动 API 服务器（daemon 线程）。

    Args:
        host: 监听地址。
        port: 监听端口。
        active_rooms: main._active_rooms 字典引用。
        active_rooms_lock: main._active_rooms_lock 引用。
        rooms_loader: 加载 rooms.txt 的函数。

    Returns:
        threading.Thread 实例。
    """
    handler_class = _make_handler(active_rooms, active_rooms_lock, rooms_loader)

    def _serve():
        try:
            server = HTTPServer((host, port), handler_class)
            logger.info(f"[API] 服务已启动: http://{host}:{port}")
            logger.info(f"[API] 端点: GET /api/status, /api/rooms, /api/rooms/:id")
            server.serve_forever()
        except Exception as e:
            logger.error(f"[API] 服务启动失败: {e}")

    t = threading.Thread(target=_serve, daemon=True, name='api-server')
    t.start()
    return t
