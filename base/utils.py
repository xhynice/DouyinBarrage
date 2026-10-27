"""基础工具：配置加载、Cookie 解析、常量定义、格式化、ID 生成。

本模块是项目的共享基础层，被 service/ 和 base/ 其他模块共同依赖。
抖音 API 参数（APP_ID、VERSION_CODE 等）集中在此维护，更新时只需改一处。
"""

__all__ = [
    'USER_AGENTS', 'APP_ID', 'LIVE_ID', 'VERSION_CODE', 'WEBCAST_SDK_VERSION',
    'DID_RULE', 'DEVICE_PLATFORM', 'LOW_VALUE_TYPES', 'INTERACTIVE_TYPES',
    'METHOD_TO_CONFIG', 'SCRIPT_DIR', 'DEFAULT_CONFIG',
    'load_config', 'load_cookies', 'update_room_name_in_config',
    'sanitize_dir_name', 'generate_user_unique_id', 'generate_ms_token',
    'extract_ua_version', 'fmt_fans_club', 'fmt_grade', 'rotate_ua',
    'get_user_id', 'get_anchor_dir', 'resolve_anchor_dir',
]

import json
import os
import logging
import random
import re
import shutil
import tempfile
import threading
import time

import yaml


# ── 常量 ──────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

# ── 签名 & API 共享参数 ───────────────────────────
# 抖音 Web 端参数，签名和 WebSocket URL 共用。
# 抖音版本更新时只需修改这里。

APP_ID = '6383'                  # 抖音 Web 端应用 ID
LIVE_ID = '1'                    # 直播类型标识（1 = 普通直播）
VERSION_CODE = '180800'          # 客户端版本号（对应 18.08.00）
WEBCAST_SDK_VERSION = '1.0.15'   # WebCast SDK 版本，签名和 WS URL 须一致
DID_RULE = '3'                   # 设备 ID 生成规则版本（3 = 当前线上版本）
DEVICE_PLATFORM = 'web'          # 平台标识

# 低频/低价值消息类型，仅计数不解析
LOW_VALUE_TYPES = frozenset({
    'WebcastRanklistHourEntranceMessage', 'WebcastRoomDataSyncMessage',
    'WebcastChatLikeMessage', 'WebcastResidentGuestMessage',
    'WebcastLowPcuGuideMessage', 'WebcastCommonDotMessage',
    'WebcastGiftUpdateMessage', 'WebcastInRoomBannerMessage',
    'WebcastNotifyEffectMessage', 'WebcastHotRoomMessage',
})

# 交互类消息，用于"等待开播"模式判断直播间是否活跃
INTERACTIVE_TYPES = frozenset({
    'WebcastChatMessage', 'WebcastGiftMessage', 'WebcastLikeMessage',
    'WebcastMemberMessage', 'WebcastSocialMessage', 'WebcastFansclubMessage',
    'WebcastEmojiChatMessage',
})

# WebSocket method → output config key 映射
# strip('Webcast','Message').lower() 后与 config key 不一致的特殊映射
METHOD_TO_CONFIG = {
    'WebcastChatMessage':                 'chat',
    'WebcastGiftMessage':                 'gift',
    'WebcastLikeMessage':                 'like',
    'WebcastMemberMessage':               'member',
    'WebcastSocialMessage':               'social',
    'WebcastRoomUserSeqMessage':          'stats',
    'WebcastFansclubMessage':             'fansclub',
    'WebcastControlMessage':              'control',
    'WebcastEmojiChatMessage':            'emoji',
    'WebcastRoomStatsMessage':            'roomstats',
    'WebcastRoomMessage':                 'room',
    'WebcastRoomRankMessage':             'rank',
    'WebcastRoomStreamAdaptationMessage': 'control',  # 无独立 config，归入 control
}

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_MIN_UA_SWITCH_INTERVAL = 8       # UA 切换最小间隔（秒），防止频繁切换触发风控
_ua_switch_lock = threading.Lock()
_last_ua_switch_time = 0.0


# ── 配置加载 ──────────────────────────────────────

def _deep_merge(base, override):
    """递归合并两个字典，override 中的值覆盖 base。

    嵌套字典递归合并，非字典类型直接覆盖。
    """
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(config_file, default_config):
    """加载 YAML 配置文件，与默认配置做深度合并。

    嵌套字典递归合并（用户未指定的子项保留默认值），
    非字典类型直接覆盖。文件不存在时返回默认配置。

    Args:
        config_file: 配置文件路径（相对路径相对于项目根目录）。
        default_config: 默认配置字典。

    Returns:
        合并后的配置字典。
    """
    if not os.path.isabs(config_file):
        config_file = os.path.join(SCRIPT_DIR, config_file)

    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            user_cfg = yaml.safe_load(f.read()) or {}
        cfg = _deep_merge(dict(default_config), user_cfg)
        return cfg
    except (FileNotFoundError, yaml.YAMLError) as e:
        print(f"配置加载失败({e})，使用默认配置")
        return dict(default_config)


def load_cookies(cookie_file, script_dir=''):
    """加载 Cookie 文件，自动识别三种格式。

    支持格式：
    - 浏览器导出：name1=value1; name2=value2
    - 每行一个：name1=value1（多行）
    - Netscape cookie jar：带 tab 分隔的 7 列格式

    Args:
        cookie_file: Cookie 文件路径。
        script_dir: 相对路径的基准目录（为空时使用项目根目录）。

    Returns:
        {cookie_name: cookie_value} 字典，文件不存在时返回空字典。
    """
    if not os.path.isabs(cookie_file):
        cookie_file = os.path.join(script_dir, cookie_file) if script_dir else cookie_file
    if not os.path.exists(cookie_file):
        return {}

    try:
        with open(cookie_file, 'r', encoding='utf-8') as f:
            content = f.read().strip()
    except Exception:
        return {}
    if not content:
        return {}

    cookies = {}
    lines = content.splitlines()
    is_netscape = any(line.count('	') >= 6 and not line.startswith('#') for line in lines[:10])

    if is_netscape:
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('	')
            if len(parts) >= 7:
                name, value = parts[5].strip(), parts[6].strip()
                if name:
                    cookies[name] = value
    else:
        for item in content.splitlines():
            item = item.strip()
            if not item:
                continue
            for part in item.split(';'):
                part = part.strip()
                if not part or '=' not in part:
                    continue
                name, value = part.split('=', 1)
                if name.strip():
                    cookies[name.strip()] = value.strip()
    return cookies


# ── 配置写回 ──────────────────────────────────────

_config_write_lock = threading.RLock()


def update_room_name_in_config(room_id, anchor_name, rooms_file='rooms.txt'):
    """更新或添加 rooms.txt 中的房间记录。

    线程安全：通过可重入锁防止多房间并发写入。
    - 房间已存在：更新主播名
    - 房间不存在：追加到文件末尾
    - 文件不存在：创建文件并写入

    Args:
        room_id: 直播间 ID。
        anchor_name: 主播昵称。
        rooms_file: 房间文件路径（相对于项目根目录）。
    """
    if not anchor_name:
        return
    if not os.path.isabs(rooms_file):
        rooms_file = os.path.join(SCRIPT_DIR, rooms_file)

    with _config_write_lock:
        try:
            if not os.path.exists(rooms_file):
                with open(rooms_file, 'w', encoding='utf-8') as f:
                    f.write(f'{room_id},{anchor_name}\n')
                return

            with open(rooms_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            updated = False
            found = False
            new_lines = []

            for line in lines:
                stripped = line.strip()
                if not stripped:
                    new_lines.append(line)
                    continue

                prefix = ''
                content = stripped
                if stripped.startswith('#'):
                    prefix = '#'
                    content = stripped[1:].strip()

                if not content:
                    new_lines.append(line)
                    continue

                parts = content.split(',', 1)
                if parts[0].strip() == room_id:
                    indent = (m.group(1) if (m := re.match(r'^(\s*)', line)) else '')
                    new_lines.append(f'{indent}{prefix}{room_id},{anchor_name}\n')
                    updated = True
                    found = True
                else:
                    new_lines.append(line)

            if not found:
                if new_lines and not new_lines[-1].endswith('\n'):
                    new_lines.append('\n')
                new_lines.append(f'{room_id},{anchor_name}\n')
                updated = True

            if updated:
                fd, temp_path = tempfile.mkstemp(suffix='.txt', dir=os.path.dirname(rooms_file))
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as f:
                        f.writelines(new_lines)
                    shutil.move(temp_path, rooms_file)
                except Exception:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    raise

        except Exception as e:
            try:
                logger = logging.getLogger(__name__)
                logger.error(f"[配置] 更新主播名失败：room_id={room_id}, error={e}")
            except Exception:
                pass


# ── 工具函数 ──────────────────────────────────────

def sanitize_dir_name(name):
    """清理目录名中的非法字符并去除首尾空格。

    Args:
        name: 原始名称。

    Returns:
        清理后的安全目录名。
    """
    return re.sub(r'[\/\\\:\*\？?\"\<\>\|\s]', '', name).strip()


def generate_user_unique_id():
    """生成随机用户唯一 ID，用于 WebSocket 连接标识。

    Returns:
        18~19 位随机数字字符串。
    """
    return str(random.randint(10**18, 10**19 - 1))


def generate_ms_token(length=182):
    """生成随机 msToken 字符串，用于 HTTP 请求参数。

    Args:
        length: token 主体长度（不含末尾 '=_' 后缀）。

    Returns:
        指定长度的随机字符串 + '=_' 后缀。
    """
    charset = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+='
    return ''.join(random.choice(charset) for _ in range(length)) + '=_'


def extract_ua_version(ua: str) -> str:
    """从 User-Agent 字符串中提取 Chrome 版本号。

    Args:
        ua: 完整的 User-Agent 字符串。

    Returns:
        'Chrome/x.x.x.x' 格式的版本字符串，无法匹配时返回默认值。
    """
    m = re.search(r'Chrome/(\d+\.\d+\.\d+\.\d+)', ua)
    return f"Chrome/{m.group(1)}" if m else "Chrome/132.0.0.0"


def fmt_fans_club(user):
    """格式化用户的粉丝团信息为显示字符串。

    Args:
        user: protobuf User 对象。

    Returns:
        '[粉丝团:名称 Lv等级]' 或 '[粉丝团 Lv等级]'，无粉丝团时返回空字符串。
    """
    try:
        club = user.fans_club.data
        if club and club.club_name:
            return f"[粉丝团:{club.club_name} Lv{club.level}]"
        elif club and club.level > 0:
            return f"[粉丝团 Lv{club.level}]"
    except (AttributeError, TypeError):
        pass
    return ''


def fmt_grade(user):
    """格式化用户的消费等级为显示字符串。

    Args:
        user: protobuf User 对象。

    Returns:
        '[等级N]' 格式字符串，等级为 0 或缺失时返回空字符串。
    """
    try:
        if user.pay_grade and user.pay_grade.level > 0:
            return f"[等级{user.pay_grade.level}]"
    except (AttributeError, TypeError):
        pass
    return ''


def rotate_ua(current_ua):
    """重连时切换 User-Agent，降低风控风险。

    两次切换间隔不足 _MIN_UA_SWITCH_INTERVAL 秒时跳过，
    避免重连密集期频繁切换反而触发异常检测。

    线程安全：多实例并发时通过锁保护全局切换时间。

    Args:
        current_ua: 当前使用的 User-Agent 字符串。

    Returns:
        (新 UA 字符串, 新 UA 版本字符串) 元组。
    """
    global _last_ua_switch_time
    with _ua_switch_lock:
        now = time.time()
        if now - _last_ua_switch_time < _MIN_UA_SWITCH_INTERVAL:
            return current_ua, extract_ua_version(current_ua)
        candidates = [u for u in USER_AGENTS if u != current_ua]
        if not candidates:
            return current_ua, extract_ua_version(current_ua)
        new_ua = random.choice(candidates)
        _last_ua_switch_time = now
        return new_ua, extract_ua_version(new_ua)


def get_user_id(user):
    """获取用户 ID 字符串，优先使用 id_str（大数精度更高）。

    Args:
        user: protobuf User 对象。

    Returns:
        用户 ID 字符串。
    """
    s = user.id_str
    return s if s else str(user.id)


def get_anchor_dir(output_dir: str, anchor_name: str, live_id: str) -> str:
    """获取主播输出目录路径（纯函数，按当前昵称算）。

    注意：运行时不要直接用它——主播一改名，新会话就会算出新目录，
    数据就分裂成两个文件夹（张君雅1v4⁸⁰⁰⁸ / 张君雅1v4（休）就是这么来的）。
    运行时请用 resolve_anchor_dir()，它以首次采集的目录为准、改名不挪窝。
    """
    dir_name = sanitize_dir_name(anchor_name) or live_id
    return os.path.join(output_dir, dir_name)


_anchor_map_lock = threading.Lock()


def _anchor_map_path(output_dir: str) -> str:
    return os.path.join(output_dir, "anchor_map.json")


def _load_anchor_map(output_dir: str) -> dict:
    """读 anchor_map.json；文件不存在/损坏都当成空表，绝不因此中断采集。"""
    try:
        with open(_anchor_map_path(output_dir), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_anchor_map(output_dir: str, mapping: dict) -> None:
    """原子写 anchor_map.json（tmp + replace，写坏不留半截文件）。"""
    try:
        os.makedirs(output_dir, exist_ok=True)
        tmp = _anchor_map_path(output_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _anchor_map_path(output_dir))
    except Exception:
        pass


def resolve_anchor_dir(output_dir: str, anchor_name: str, live_id: str) -> str:
    """已采集主播的稳定输出目录：首次采集定下来的目录名，此后改名也不变。

    规则（按优先级）：
    1. anchor_map.json 里已有该 live_id → 直接用记录的目录，只更新里面
       的 nickname（展示用），目录名不动；
    2. 没有记录，但已有某个子目录的 meta.json 里 live_id 就是这个主播
       （升级前的老数据，如张君雅1v4⁸⁰⁰⁸）→ 认领那个老目录，不另建新目录；
    3. 都没有 → 用当前昵称建目录；若该名字已被别的 live_id 占用，
       后缀 _<live_id> 区分，避免两个主播写进同一个文件夹。
    live_id 是 rooms.txt 里的配置 id（主播数字 id），改名不会变，是比
    昵称可靠得多的键。昵称只用来做“首次”目录名和展示，不再当主键。
    """
    live_id = str(live_id or "")
    current = sanitize_dir_name(anchor_name or "") or live_id
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with _anchor_map_lock:
        mapping = _load_anchor_map(output_dir)
        entry = mapping.get(live_id) if live_id else None
        if entry and entry.get("folder"):
            folder = entry["folder"]
            if entry.get("nickname") != anchor_name:
                entry["nickname"] = anchor_name
                entry["updated"] = now
                _save_anchor_map(output_dir, mapping)
            return os.path.join(output_dir, folder)

        # 升级认领：扫描已有子目录的 meta.json，找 live_id 相同的老目录。
        adopted = None
        if live_id and os.path.isdir(output_dir):
            try:
                for name in os.listdir(output_dir):
                    sub = os.path.join(output_dir, name)
                    if not os.path.isdir(sub):
                        continue
                    meta_path = os.path.join(sub, "meta.json")
                    if not os.path.isfile(meta_path):
                        continue
                    try:
                        with open(meta_path, encoding="utf-8") as f:
                            meta = json.load(f)
                    except Exception:
                        continue
                    if str(meta.get("live_id", "")) == live_id:
                        adopted = name
                        break
            except OSError:
                pass
        if adopted:
            folder = adopted
        else:
            folder = current
            # 当前名已被别的 live_id 占用时后缀区分（不写进别人的目录）。
            candidate = os.path.join(output_dir, folder)
            if os.path.isdir(candidate):
                meta_path = os.path.join(candidate, "meta.json")
                owner = None
                try:
                    with open(meta_path, encoding="utf-8") as f:
                        owner = str(json.load(f).get("live_id", ""))
                except Exception:
                    owner = None
                if owner and owner != live_id:
                    folder = f"{current}_{live_id}"
        if live_id:
            mapping[live_id] = {
                "folder": folder,
                "nickname": anchor_name,
                "first_seen": now,
                "updated": now,
            }
            _save_anchor_map(output_dir, mapping)
        return os.path.join(output_dir, folder)


# ── 默认配置 ──────────────────────────────────────
# 与 config.yaml 做浅合并时的基准配置。
# main.py 和 fetcher.py 共用此定义，保持一致。

DEFAULT_CONFIG = {
    'log_level': 'INFO',
    'output_dir': 'data',
    'live_stop': True,
    'live_check_interval': 160,
    'output': {
        'chat': True, 'lucky_bag': True, 'gift': True, 'like': True,
        'member': False, 'social': True, 'rank': False, 'stats': True,
        'fansclub': False, 'emoji': False, 'room': False, 'roomstats': False,
        'control': True,
    },
    'barrage': {
        'csv': True, 'sqlite': False,
    },
    'record': {
        'enabled': False,
        'format': 'ts',
        'quality': '原画',
        'segment_time': 0,
        'segment_size': 0,
        'auto_convert': True,
    },
    'api': {
        'enabled': False,
        'host': '0.0.0.0',
        'port': 8088,
    },
}
