"""消息解析器：Protobuf 消息反序列化与结构化分发。

每个 parse_* 函数接收 protobuf payload bytes，返回结果列表。
结果字典结构：
    type: 消息类型标识（如 'chat'、'gift'），用于 CSV 文件路由。
    msg: 人类可读的日志文本。
    data: CSV 行数据字典（与 output.py 的 CSV_FIELDS 对应）。
    action: 可选的控制指令（'stop' 终止采集、'wait_live' 等待开播）。

HANDLERS 字典将 WebSocket method 名（如 'WebcastChatMessage'）
映射到对应的解析函数，供 fetcher.py 消息分发使用。
"""

import logging
import time

from base.messages import (
    parse_proto,
    ChatMessage, GiftMessage, LikeMessage, MemberMessage,
    SocialMessage, RoomUserSeqMessage, FansclubMessage,
    ControlMessage, EmojiChatMessage, RoomStatsMessage,
    RoomMessage, RoomRankMessage, RoomStreamAdaptationMessage,
)
from base.utils import (
    get_user_id, fmt_grade, fmt_fans_club, safe_time,
)

logger = logging.getLogger(__name__)


# ── 解析函数 ──────────────────────────────────────

def parse_chat_msg(payload, enable_outputs=None):
    """解析聊天消息（含福袋口令）。

    chat_by == 9 时归类为福袋口令，否则为普通弹幕。

    Args:
        payload: ChatMessage protobuf 序列化字节。
        enable_outputs: 输出开关字典，key='chat' / 'lucky_bag' 控制是否输出。

    Returns:
        结果字典列表。类型为 'chat' 或 'lucky_bag'。
    """
    msg = parse_proto(ChatMessage, payload)
    user = msg.user
    uid = get_user_id(user)
    common = {
        'time': safe_time(msg.common.create_time if msg.common else 0) or time.strftime('%H:%M:%S'),
        'user_id': uid, 'user_name': user.nick_name,
        'grade': fmt_grade(user), 'fans_club': fmt_fans_club(user),
    }

    results = []
    if msg.chat_by == 9:  # 福袋口令
        if enable_outputs.get('lucky_bag', True):
            results.append({
                'type': 'lucky_bag',
                'msg': f"[福袋口令] {user.nick_name}[{uid}] 内容:{msg.content}",
                'data': {**common, 'content': msg.content},
            })
    else:  # 普通聊天
        if enable_outputs.get('chat', True):
            results.append({
                'type': 'chat',
                'msg': f"[聊天] {user.nick_name}[{uid}] 内容:{msg.content}",
                'data': {**common, 'content': msg.content},
            })
    return results


def parse_gift_msg(payload, enable_outputs=None):
    """解析礼物消息。

    抖币计算：gift.diamond_count × combo_count（或 total_count / repeat_count）。
    全部为 0 时不显示抖币信息。

    Args:
        payload: GiftMessage protobuf 序列化字节。
        enable_outputs: 输出开关字典，key='gift' 控制是否输出。

    Returns:
        结果字典列表。类型为 'gift'。
    """
    if not enable_outputs.get('gift', True):
        return []
    msg = parse_proto(GiftMessage, payload)
    user = msg.user
    uid = get_user_id(user)
    gift = msg.gift
    cnt = msg.combo_count or msg.total_count or msg.repeat_count or 1
    diamond_total = gift.diamond_count * cnt
    diamond_info = f" ({diamond_total}钻石)" if diamond_total > 0 else ""
    return [{
        'type': 'gift',
        'msg': f"[礼物] {user.nick_name}[{uid}] 礼物:{gift.name} x{cnt}{diamond_info}",
        'data': {
            'time': time.strftime('%H:%M:%S'),
            'user_id': uid, 'user_name': user.nick_name, 'gift_name': gift.name,
            'gift_count': cnt, 'diamond_total': diamond_total,
            'grade': fmt_grade(user), 'fans_club': fmt_fans_club(user),
        },
    }]


def parse_like_msg(payload, enable_outputs=None):
    """解析点赞消息。

    Args:
        payload: LikeMessage protobuf 序列化字节。
        enable_outputs: 输出开关字典，key='like' 控制是否输出。

    Returns:
        结果字典列表。类型为 'like'，data 含 'count'（本次）和 'total'（累计）。
    """
    if not enable_outputs.get('like', True):
        return []
    msg = parse_proto(LikeMessage, payload)
    user = msg.user
    uid = get_user_id(user)
    return [{
        'type': 'like',
        'msg': f"[点赞] {user.nick_name}[{uid}] 点赞:{msg.count}个, 累计{msg.total}赞",
        'data': {
            'time': time.strftime('%H:%M:%S'),
            'user_id': uid, 'user_name': user.nick_name,
            'count': msg.count, 'total': msg.total,
            'grade': fmt_grade(user), 'fans_club': fmt_fans_club(user),
        },
    }]


def parse_member_msg(payload, enable_outputs=None):
    """解析进场消息。

    Args:
        payload: MemberMessage protobuf 序列化字节。
        enable_outputs: 输出开关字典，key='member' 控制是否输出。

    Returns:
        结果字典列表。类型为 'member'，data 含 'gender' 和 'member_count'。
    """
    if not enable_outputs.get('member', True):
        return []
    msg = parse_proto(MemberMessage, payload)
    user = msg.user
    uid = get_user_id(user)
    gender = {0: "未知", 1: "男", 2: "女"}.get(user.gender, "未知")
    extras = f" (直播间人数:{msg.member_count})" if msg.member_count else ""
    return [{
        'type': 'member',
        'msg': f"[进场] {user.nick_name}[{uid}][{gender}] 进入了直播间{extras}",
        'data': {
            'time': time.strftime('%H:%M:%S'),
            'user_id': uid, 'user_name': user.nick_name, 'gender': gender,
            'grade': fmt_grade(user), 'fans_club': fmt_fans_club(user),
            'member_count': msg.member_count,
        },
    }]


def parse_social_msg(payload, enable_outputs=None):
    """解析关注/分享消息（仅记录关注 action=1）。

    Args:
        payload: SocialMessage protobuf 序列化字节。
        enable_outputs: 输出开关字典，key='social' 控制是否输出。

    Returns:
        结果字典列表。类型为 'social'，非关注动作（action != 1）返回空列表。
    """
    if not enable_outputs.get('social', True):
        return []
    msg = parse_proto(SocialMessage, payload)
    if msg.action != 1:
        return []
    user = msg.user
    uid = get_user_id(user)
    action = {1: "关注了主播", 2: "分享了直播间"}.get(msg.action, "互动")
    follow = f"(第{msg.follow_count}个关注)" if msg.follow_count else ""
    return [{
        'type': 'social',
        'msg': f"[关注/分享] {user.nick_name}[{uid}] {action} {follow}",
        'data': {
            'time': time.strftime('%H:%M:%S'),
            'user_id': uid, 'user_name': user.nick_name, 'action': action,
            'follow_count': msg.follow_count or '',
            'grade': fmt_grade(user), 'fans_club': fmt_fans_club(user),
        },
    }]


def parse_room_user_seq_msg(payload, enable_outputs=None):
    """解析直播间实时统计消息（在线人数等）。

    Args:
        payload: RoomUserSeqMessage protobuf 序列化字节。
        enable_outputs: 输出开关字典，key='stats' 控制是否输出。

    Returns:
        结果字典列表。类型为 'stats'，data 含 'current'、'total_pv' 等字段。
    """
    if not enable_outputs.get('stats', True):
        return []
    msg = parse_proto(RoomUserSeqMessage, payload)
    parts = [f"当前: {msg.total}"]
    if msg.total_pv_for_anchor:
        parts.append(f"累计: {msg.total_pv_for_anchor}")
    if msg.total_user_str:
        parts.append(f"累计用户: {msg.total_user_str}")
    if msg.online_user_for_anchor:
        parts.append(f"主播端在线: {msg.online_user_for_anchor}")
    return [{
        'type': 'stats',
        'msg': f"[统计] {', '.join(parts)}",
        'data': {
            'time': time.strftime('%H:%M:%S'),
            'current': msg.total,
            'total_pv': msg.total_pv_for_anchor or '',
            'total_user': msg.total_user_str or '',
            'online_anchor': msg.online_user_for_anchor or '',
        },
    }]


def parse_fansclub_msg(payload, enable_outputs=None):
    """解析粉丝团消息（加入/升级）。

    Args:
        payload: FansclubMessage protobuf 序列化字节。
        enable_outputs: 输出开关字典，key='fansclub' 控制是否输出。

    Returns:
        结果字典列表。类型为 'fansclub'。
    """
    if not enable_outputs.get('fansclub', True):
        return []
    msg = parse_proto(FansclubMessage, payload)
    user = msg.user
    uid = get_user_id(user)
    t = {1: "升级", 2: "加入"}.get(msg.type, "变动")
    return [{
        'type': 'fansclub',
        'msg': f"[粉丝团] {user.nick_name}[{uid}] {t}: {msg.content}",
        'data': {
            'time': time.strftime('%H:%M:%S'),
            'user_id': uid, 'user_name': user.nick_name,
            'type': t, 'content': msg.content,
            'grade': fmt_grade(user), 'fans_club': fmt_fans_club(user),
        },
    }]


def parse_emoji_chat_msg(payload, enable_outputs=None):
    """解析表情消息。

    Args:
        payload: EmojiChatMessage protobuf 序列化字节。
        enable_outputs: 输出开关字典，key='emoji' 控制是否输出。

    Returns:
        结果字典列表。类型为 'emoji'，无默认内容时显示 '[表情{emoji_id}]'。
    """
    if not enable_outputs.get('emoji', True):
        return []
    msg = parse_proto(EmojiChatMessage, payload)
    user = msg.user
    uid = get_user_id(user)
    content = msg.default_content or f"[表情{msg.emoji_id}]"
    return [{
        'type': 'emoji',
        'msg': f"[表情] {user.nick_name}[{uid}]: {content}",
        'data': {
            'time': time.strftime('%H:%M:%S'),
            'user_id': uid, 'user_name': user.nick_name,
            'emoji_id': msg.emoji_id, 'content': content,
            'grade': fmt_grade(user), 'fans_club': fmt_fans_club(user),
        },
    }]


def parse_room_msg(payload, enable_outputs=None):
    """解析直播间公告消息（置顶、场景等）。

    Args:
        payload: RoomMessage protobuf 序列化字节。
        enable_outputs: 输出开关字典，key='room' 控制是否输出。

    Returns:
        结果字典列表。类型为 'room'。
    """
    if not enable_outputs.get('room', True):
        return []
    msg = parse_proto(RoomMessage, payload)
    is_top = "[置顶]" if msg.system_top_msg else ""
    detail = f"直播间id:{msg.common.room_id}"
    if msg.content:
        detail += f", 内容:{msg.content}"
    if msg.biz_scene:
        detail += f", 场景:{msg.biz_scene}"
    return [{
        'type': 'room',
        'msg': f"[直播间] {is_top}{detail}",
        'data': {
            'time': time.strftime('%H:%M:%S'),
            'is_top': '是' if msg.system_top_msg else '否',
            'room_id': msg.common.room_id if msg.common else '',
            'content': msg.content or '',
            'biz_scene': msg.biz_scene or '',
        },
    }]


def parse_room_stats_msg(payload, enable_outputs=None):
    """解析直播累计统计消息（观看人次等）。

    Args:
        payload: RoomStatsMessage protobuf 序列化字节。
        enable_outputs: 输出开关字典，key='roomstats' 控制是否输出。

    Returns:
        结果字典列表。类型为 'roomstats'。
    """
    if not enable_outputs.get('roomstats', True):
        return []
    msg = parse_proto(RoomStatsMessage, payload)
    detail = msg.display_long or msg.display_middle or msg.display_short or str(msg.total)
    return [{
        'type': 'roomstats',
        'msg': f"[直播统计] {detail} (数值:{msg.total})",
        'data': {
            'time': time.strftime('%H:%M:%S'),
            'detail': detail,
            'total': msg.total,
        },
    }]


def parse_rank_msg(payload, enable_outputs=None):
    """解析排行榜消息。

    Args:
        payload: RoomRankMessage protobuf 序列化字节。
        enable_outputs: 输出开关字典，key='rank' 控制是否输出。

    Returns:
        结果字典列表。类型为 'rank'，无有效分数时返回空列表。
    """
    if not enable_outputs.get('rank', True):
        return []
    msg = parse_proto(RoomRankMessage, payload)
    if not msg.ranks_list:
        return []
    items = [f"{i}.{r.user.nick_name}{fmt_fans_club(r.user)} 积分:{r.score_str}"
             for i, r in enumerate(msg.ranks_list, 1) if r.score_str]
    if items:
        return [{
            'type': 'rank',
            'msg': f"[排行榜] {' | '.join(items)}",
            'data': {
                'time': time.strftime('%H:%M:%S'),
                'ranks': ' | '.join(items),
            },
        }]
    return []


def parse_control_msg(payload, enable_outputs=None):
    """解析直播控制消息（开始/暂停/结束）。

    直播结束时根据 live_stop 配置返回控制指令：
    - live_stop=False → action='wait_live'（进入等待开播模式）
    - live_stop=True  → action='stop'（终止采集）

    Args:
        payload: ControlMessage protobuf 序列化字节。
        enable_outputs: 输出开关字典，key='control' 控制是否记录，
            key='live_stop' 决定结束时的行为。

    Returns:
        结果字典列表。始终包含 status='已结束' 时的 action 指令。
    """
    msg = parse_proto(ControlMessage, payload)
    status = {1: "开始", 2: "暂停", 3: "已结束"}.get(msg.status, f"未知({msg.status})")
    results = []
    if enable_outputs.get('control', True):
        results.append({
            'type': 'control',
            'msg': f"[直播状态] {status}",
            'data': {
                'time': time.strftime('%H:%M:%S'),
                'status': status,
            },
        })
    if msg.status == 3:
        if enable_outputs.get('live_stop', False):
            results.append({'action': 'stop'})
        else:
            results.append({'action': 'wait_live'})
    return results


def parse_room_stream_adaptation_msg(payload, enable_outputs=None):
    """解析流配置消息（仅日志，不写入数据文件）。

    Args:
        payload: RoomStreamAdaptationMessage protobuf 序列化字节。
        enable_outputs: 未使用，保持签名一致。

    Returns:
        类型为 '_log_only' 的结果列表，不会被写入 CSV/JSONL。
    """
    msg = parse_proto(RoomStreamAdaptationMessage, payload)
    return [{
        'type': '_log_only',
        'msg': f"[流配置] 类型:{msg.adaptation_type}",
    }]


# ── 分发表 ──────────────────────────────────────

HANDLERS = {
    'WebcastChatMessage':                 parse_chat_msg,
    'WebcastGiftMessage':                 parse_gift_msg,
    'WebcastLikeMessage':                 parse_like_msg,
    'WebcastMemberMessage':               parse_member_msg,
    'WebcastSocialMessage':               parse_social_msg,
    'WebcastRoomUserSeqMessage':          parse_room_user_seq_msg,
    'WebcastFansclubMessage':             parse_fansclub_msg,
    'WebcastControlMessage':              parse_control_msg,
    'WebcastEmojiChatMessage':            parse_emoji_chat_msg,
    'WebcastRoomStatsMessage':            parse_room_stats_msg,
    'WebcastRoomMessage':                 parse_room_msg,
    'WebcastRoomRankMessage':             parse_rank_msg,
    'WebcastRoomStreamAdaptationMessage': parse_room_stream_adaptation_msg,
}
