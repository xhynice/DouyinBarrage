"""Protobuf 消息定义：抖音直播间 WebSocket 传输层和业务层消息。

本模块使用 proto-plus 定义所有消息结构，由 parser.py 的 parse_proto 函数反序列化。
消息定义来自抖音 Web 端 WebSocket 协议逆向工程。

结构分层：
    传输层    PushFrame / Response — WebSocket 帧封装
    通用层    Common / User / Image / Text — 被多个业务消息复用
    业务层    ChatMessage / GiftMessage 等 — 对应 13 种消息类型
    辅助层    Kk / Rsp / RspF 等 — 逆向所得含义不明的加密字段
"""

import proto


# ── 常量枚举 ──────────────────────────────────────

class CommentTypeTag:
    """评论类型标签（用于过滤系统消息）。"""
    COMMENTTYPETAGUNKNOWN = 0
    COMMENTTYPETAGSTAR = 1


class RoomMsgTypeEnum:
    """直播间消息子类型（RoomMessage.roommessagetype 的枚举值）。"""
    DEFAULTROOMMSG = 0
    ECOMLIVEREPLAYSAVEROOMMSG = 1
    CONSUMERRELATIONROOMMSG = 2
    JUMANJIDATAAUTHNOTIFYMSG = 3
    VSWELCOMEMSG = 4
    MINORREFUNDMSG = 5
    PAIDLIVEROOMNOTIFYANCHORMSG = 6
    HOSTTEAMSYSTEMMSG = 7


# ── 传输层 ────────────────────────────────────────

class Message(proto.Message):
    """WebSocket 单条消息封装（嵌入在 PushFrame.payload 解压后的 Response 中）。

    Attributes:
        method: 消息类型标识（如 'WebcastChatMessage'），用于分发到对应 handler。
        payload: protobuf 序列化的业务消息体。
        msg_id: 消息唯一 ID。
    """
    method = proto.Field(proto.STRING, number=1)
    payload = proto.Field(proto.BYTES, number=2)
    msg_id = proto.Field(proto.INT64, number=3)
    msg_type = proto.Field(proto.INT32, number=4)
    offset = proto.Field(proto.INT64, number=5)
    need_wrds_store = proto.Field(proto.BOOL, number=6)
    wrds_version = proto.Field(proto.INT64, number=7)
    wrds_sub_key = proto.Field(proto.STRING, number=8)


class Response(proto.Message):
    """WebSocket 响应体，包含消息列表和 ACK 控制字段。

    Attributes:
        messages_list: 消息列表，每条对应一个业务消息。
        need_ack: 是否需要回送 ACK（通过 PushFrame payload_type='ack'）。
        internal_ext: ACK 回送的数据载荷。
    """
    messages_list = proto.RepeatedField(Message, number=1)
    cursor = proto.Field(proto.STRING, number=2)
    fetch_interval = proto.Field(proto.UINT64, number=3)
    now = proto.Field(proto.UINT64, number=4)
    internal_ext = proto.Field(proto.STRING, number=5)
    fetch_type = proto.Field(proto.UINT32, number=6)
    route_params = proto.MapField(proto.STRING, proto.STRING, number=7)
    heartbeat_duration = proto.Field(proto.UINT64, number=8)
    need_ack = proto.Field(proto.BOOL, number=9)
    push_server = proto.Field(proto.STRING, number=10)
    live_cursor = proto.Field(proto.STRING, number=11)
    history_no_more = proto.Field(proto.BOOL, number=12)


class HeadersList(proto.Message):
    """通用键值对头部字段。"""
    key = proto.Field(proto.STRING, number=1)
    value = proto.Field(proto.STRING, number=2)


class PushFrame(proto.Message):
    """WebSocket 传输帧，心跳包和消息包共用此结构。

    Attributes:
        payload_type: 帧类型，'hb' 表示心跳包，其他为消息包。
        payload: gzip 压缩的 Response 序列化数据（消息包）。
        log_id: 帧标识，ACK 时需回传。
    """
    seq_id = proto.Field(proto.UINT64, number=1)
    log_id = proto.Field(proto.UINT64, number=2)
    service = proto.Field(proto.UINT64, number=3)
    method = proto.Field(proto.UINT64, number=4)
    headers_list = proto.RepeatedField(HeadersList, number=5)
    payload_encoding = proto.Field(proto.STRING, number=6)
    payload_type = proto.Field(proto.STRING, number=7)
    payload = proto.Field(proto.BYTES, number=8)


# ── 通用层：Image / Text / User / Common ─────────

class Image(proto.Message):
    """图片资源，包含多分辨率 URL 列表。"""
    url_list_list = proto.RepeatedField(proto.STRING, number=1)
    uri = proto.Field(proto.STRING, number=2)
    height = proto.Field(proto.UINT64, number=3)
    width = proto.Field(proto.UINT64, number=4)
    avg_color = proto.Field(proto.STRING, number=5)
    image_type = proto.Field(proto.UINT32, number=6)
    open_web_url = proto.Field(proto.STRING, number=7)
    content = proto.Field("ImageContent", number=8)
    is_animated = proto.Field(proto.BOOL, number=9)
    flex_setting_list = proto.Field("NinePatchSetting", number=10)
    text_setting_list = proto.Field("NinePatchSetting", number=11)


class NinePatchSetting(proto.Message):
    """九宫格图片设置。"""
    setting_list_list = proto.RepeatedField(proto.STRING, number=1)


class ImageContent(proto.Message):
    """图片内嵌文本内容。"""
    name = proto.Field(proto.STRING, number=1)
    font_color = proto.Field(proto.STRING, number=2)
    level = proto.Field(proto.UINT64, number=3)
    alternative_text = proto.Field(proto.STRING, number=4)


class TextFormat(proto.Message):
    """文本格式（颜色、加粗、字号等）。"""
    color = proto.Field(proto.STRING, number=1)
    bold = proto.Field(proto.BOOL, number=2)
    italic = proto.Field(proto.BOOL, number=3)
    weight = proto.Field(proto.UINT32, number=4)
    italic_angle = proto.Field(proto.UINT32, number=5)
    font_size = proto.Field(proto.UINT32, number=6)
    use_heigh_light_color = proto.Field(proto.BOOL, number=7)
    use_remote_clor = proto.Field(proto.BOOL, number=8)


class TextPieceUser(proto.Message):
    """富文本中的用户引用片段。"""
    user = proto.Field("User", number=1)
    with_colon = proto.Field(proto.BOOL, number=2)


class TextPieceGift(proto.Message):
    """富文本中的礼物引用片段。"""
    gift_id = proto.Field(proto.UINT64, number=1)
    name_ref = proto.Field("PatternRef", number=2)


class PatternRef(proto.Message):
    """模板引用（用于动态拼接文本）。"""
    key = proto.Field(proto.STRING, number=1)
    default_pattern = proto.Field(proto.STRING, number=2)


class TextPieceHeart(proto.Message):
    """富文本中的爱心片段。"""
    color = proto.Field(proto.STRING, number=1)


class TextPiecePatternRef(proto.Message):
    """富文本中的模板引用片段。"""
    key = proto.Field(proto.STRING, number=1)
    default_pattern = proto.Field(proto.STRING, number=2)


class TextPieceImage(proto.Message):
    """富文本中的图片片段。"""
    image = proto.Field(Image, number=1)
    scaling_rate = proto.Field(proto.FLOAT, number=2)


class TextPiece(proto.Message):
    """富文本的单个片段（字符串 / 用户 / 礼物 / 图片等）。"""
    type = proto.Field(proto.BOOL, number=1)
    format = proto.Field(TextFormat, number=2)
    string_value = proto.Field(proto.STRING, number=3)
    user_value = proto.Field(TextPieceUser, number=4)
    gift_value = proto.Field(TextPieceGift, number=5)
    heart_value = proto.Field(TextPieceHeart, number=6)
    pattern_ref_value = proto.Field(TextPiecePatternRef, number=7)
    image_value = proto.Field(TextPieceImage, number=8)


class Text(proto.Message):
    """富文本容器，由多个 TextPiece 拼接而成。"""
    key = proto.Field(proto.STRING, number=1)
    default_patter = proto.Field(proto.STRING, number=2)
    default_format = proto.Field(TextFormat, number=3)
    pieces_list = proto.RepeatedField(TextPiece, number=4)


class FollowInfo(proto.Message):
    """用户关注信息。"""
    following_count = proto.Field(proto.UINT64, number=1)
    follower_count = proto.Field(proto.UINT64, number=2)
    follow_status = proto.Field(proto.UINT64, number=3)
    push_status = proto.Field(proto.UINT64, number=4)
    remark_name = proto.Field(proto.STRING, number=5)
    follower_count_str = proto.Field(proto.STRING, number=6)
    following_count_str = proto.Field(proto.STRING, number=7)


class GradeIcon(proto.Message):
    """等级图标。"""
    icon = proto.Field(Image, number=1)
    icon_diamond = proto.Field(proto.INT64, number=2)
    level = proto.Field(proto.INT64, number=3)
    level_str = proto.Field(proto.STRING, number=4)


class GradeBuffInfo(proto.Message):
    """等级增益信息。"""
    pass


class PayGrade(proto.Message):
    """用户消费等级详情。"""
    total_diamond_count = proto.Field(proto.INT64, number=1)
    diamond_icon = proto.Field(Image, number=2)
    name = proto.Field(proto.STRING, number=3)
    icon = proto.Field(Image, number=4)
    next_name = proto.Field(proto.STRING, number=5)
    level = proto.Field(proto.INT64, number=6)
    next_icon = proto.Field(Image, number=7)
    next_diamond = proto.Field(proto.INT64, number=8)
    now_diamond = proto.Field(proto.INT64, number=9)
    this_grade_min_diamond = proto.Field(proto.INT64, number=10)
    this_grade_max_diamond = proto.Field(proto.INT64, number=11)
    pay_diamond_bak = proto.Field(proto.INT64, number=12)
    grade_describe = proto.Field(proto.STRING, number=13)
    grade_icon_list = proto.RepeatedField(GradeIcon, number=14)
    screen_chat_type = proto.Field(proto.INT64, number=15)
    im_icon = proto.Field(Image, number=16)
    im_icon_with_level = proto.Field(Image, number=17)
    live_icon = proto.Field(Image, number=18)
    new_im_icon_with_level = proto.Field(Image, number=19)
    new_live_icon = proto.Field(Image, number=20)
    upgrade_need_consume = proto.Field(proto.INT64, number=21)
    next_privileges = proto.Field(proto.STRING, number=22)
    background = proto.Field(Image, number=23)
    background_back = proto.Field(Image, number=24)
    score = proto.Field(proto.INT64, number=25)
    buff_info = proto.Field(GradeBuffInfo, number=26)
    grade_banner = proto.Field(proto.STRING, number=1001)
    profile_dialog_bg = proto.Field(Image, number=1002)
    profile_dialog_bg_back = proto.Field(Image, number=1003)


class UserBadge(proto.Message):
    """用户徽章。"""
    icons = proto.MapField(proto.INT32, Image, number=1)
    title = proto.Field(proto.STRING, number=2)


class FansClubData(proto.Message):
    """粉丝团数据（单个主播的粉丝团）。"""
    club_name = proto.Field(proto.STRING, number=1)
    level = proto.Field(proto.INT32, number=2)
    user_fans_club_status = proto.Field(proto.INT32, number=3)
    badge = proto.Field(UserBadge, number=4)
    available_gift_ids = proto.RepeatedField(proto.INT64, number=5)
    anchor_id = proto.Field(proto.INT64, number=6)


class FansClub(proto.Message):
    """粉丝团容器（支持多主播粉丝团）。"""
    data = proto.Field(FansClubData, number=1)
    prefer_data = proto.MapField(proto.INT32, FansClubData, number=2)


class User(proto.Message):
    """用户信息，被所有业务消息引用。

    关键字段：id / id_str（用户 ID）、nick_name（昵称）、
    pay_grade（消费等级）、fans_club（粉丝团）。
    """
    id = proto.Field(proto.UINT64, number=1)
    short_id = proto.Field(proto.UINT64, number=2)
    nick_name = proto.Field(proto.STRING, number=3)
    gender = proto.Field(proto.UINT32, number=4)
    signature = proto.Field(proto.STRING, number=5)
    level = proto.Field(proto.UINT32, number=6)
    birthday = proto.Field(proto.UINT64, number=7)
    telephone = proto.Field(proto.STRING, number=8)
    avatar_thumb = proto.Field(Image, number=9)
    avatar_medium = proto.Field(Image, number=10)
    avatar_large = proto.Field(Image, number=11)
    verified = proto.Field(proto.BOOL, number=12)
    experience = proto.Field(proto.UINT32, number=13)
    city = proto.Field(proto.STRING, number=14)
    status = proto.Field(proto.INT32, number=15)
    create_time = proto.Field(proto.UINT64, number=16)
    modify_time = proto.Field(proto.UINT64, number=17)
    secret = proto.Field(proto.UINT32, number=18)
    share_qrcode_uri = proto.Field(proto.STRING, number=19)
    income_share_percent = proto.Field(proto.UINT32, number=20)
    badge_image_list = proto.RepeatedField(Image, number=21)
    follow_info = proto.Field(FollowInfo, number=22)
    pay_grade = proto.Field(PayGrade, number=23)
    fans_club = proto.Field(FansClub, number=24)
    special_id = proto.Field(proto.STRING, number=26)
    avatar_border = proto.Field(Image, number=27)
    medal = proto.Field(Image, number=28)
    real_time_icons_list = proto.RepeatedField(Image, number=29)
    display_id = proto.Field(proto.STRING, number=38)
    sec_uid = proto.Field(proto.STRING, number=46)
    fan_ticket_count = proto.Field(proto.UINT64, number=1022)
    id_str = proto.Field(proto.STRING, number=1028)
    age_range = proto.Field(proto.UINT32, number=1045)


class Common(proto.Message):
    """公共字段，被所有业务消息引用。

    关键字段：room_id（直播间 ID）、create_time（创建时间戳，秒级）、
    method（消息类型标识）。
    """
    method = proto.Field(proto.STRING, number=1)
    msg_id = proto.Field(proto.UINT64, number=2)
    room_id = proto.Field(proto.UINT64, number=3)
    create_time = proto.Field(proto.UINT64, number=4)
    monitor = proto.Field(proto.UINT32, number=5)
    is_show_msg = proto.Field(proto.BOOL, number=6)
    describe = proto.Field(proto.STRING, number=7)
    fold_type = proto.Field(proto.UINT64, number=9)
    anchor_fold_type = proto.Field(proto.UINT64, number=10)
    priority_score = proto.Field(proto.UINT64, number=11)
    log_id = proto.Field(proto.STRING, number=12)
    msg_process_filter_k = proto.Field(proto.STRING, number=13)
    msg_process_filter_v = proto.Field(proto.STRING, number=14)
    user = proto.Field(User, number=15)
    anchor_fold_type_v2 = proto.Field(proto.UINT64, number=17)
    process_at_sei_time_ms = proto.Field(proto.UINT64, number=18)
    random_dispatch_ms = proto.Field(proto.UINT64, number=19)
    is_dispatch = proto.Field(proto.BOOL, number=20)
    channel_id = proto.Field(proto.UINT64, number=21)
    diff_sei2abs_second = proto.Field(proto.UINT64, number=22)
    anchor_fold_duration = proto.Field(proto.UINT64, number=23)


class PublicAreaCommon(proto.Message):
    """公共区域通用字段（消费等级、送礼数等标签）。"""
    user_label = proto.Field(Image, number=1)
    user_consume_in_room = proto.Field(proto.UINT64, number=2)
    user_send_gift_cnt_in_room = proto.Field(proto.UINT64, number=3)


class LandscapeAreaCommon(proto.Message):
    """横屏模式下的公共区域字段。"""
    show_head = proto.Field(proto.BOOL, number=1)
    show_nickname = proto.Field(proto.BOOL, number=2)
    show_font_color = proto.Field(proto.BOOL, number=3)
    color_value_list = proto.RepeatedField(proto.STRING, number=4)
    comment_type_tags_list = proto.RepeatedField(proto.INT32, number=5)


class EffectConfig(proto.Message):
    """进场/关注等特效配置。"""
    type = proto.Field(proto.UINT64, number=1)
    icon = proto.Field(Image, number=2)
    avatar_pos = proto.Field(proto.UINT64, number=3)
    text = proto.Field(Text, number=4)
    text_icon = proto.Field(Image, number=5)
    stay_time = proto.Field(proto.UINT32, number=6)
    anim_asset_id = proto.Field(proto.UINT64, number=7)
    badge = proto.Field(Image, number=8)
    flex_setting_array_list = proto.RepeatedField(proto.UINT64, number=9)
    text_icon_overlay = proto.Field(Image, number=10)
    animated_badge = proto.Field(Image, number=11)
    has_sweep_light = proto.Field(proto.BOOL, number=12)
    text_flex_setting_array_list = proto.RepeatedField(proto.UINT64, number=13)
    center_anim_asset_id = proto.Field(proto.UINT64, number=14)
    dynamic_image = proto.Field(Image, number=15)
    extra_map = proto.MapField(proto.STRING, proto.STRING, number=16)
    mp4_anim_asset_id = proto.Field(proto.UINT64, number=17)
    priority = proto.Field(proto.UINT64, number=18)
    max_wait_time = proto.Field(proto.UINT64, number=19)
    dress_id = proto.Field(proto.STRING, number=20)
    alignment = proto.Field(proto.UINT64, number=21)
    alignment_offset = proto.Field(proto.UINT64, number=22)


# ── 业务层：13 种消息类型 ────────────────────────

class ChatMessage(proto.Message):
    """弹幕消息（含福袋口令）。

    chat_by == 9 时为福袋口令，其他值为普通弹幕。
    """
    common = proto.Field(Common, number=1)
    user = proto.Field(User, number=2)
    content = proto.Field(proto.STRING, number=3)
    visible_to_sender = proto.Field(proto.BOOL, number=4)
    background_image = proto.Field(Image, number=5)
    full_screen_text_color = proto.Field(proto.STRING, number=6)
    background_image_v2 = proto.Field(Image, number=7)
    public_area_common = proto.Field(PublicAreaCommon, number=9)
    gift_image = proto.Field(Image, number=10)
    agree_msg_id = proto.Field(proto.UINT64, number=11)
    priority_level = proto.Field(proto.UINT32, number=12)
    landscape_area_common = proto.Field(LandscapeAreaCommon, number=13)
    event_time = proto.Field(proto.UINT64, number=15)
    send_review = proto.Field(proto.BOOL, number=16)
    from_intercom = proto.Field(proto.BOOL, number=17)
    intercom_hide_user_card = proto.Field(proto.BOOL, number=18)
    chat_by = proto.Field(proto.UINT32, number=20)
    individual_chat_priority = proto.Field(proto.UINT32, number=21)
    rtf_content = proto.Field(Text, number=22)


class EmojiChatMessage(proto.Message):
    """表情消息（emoji_id + 可选默认文本）。"""
    common = proto.Field(Common, number=1)
    user = proto.Field(User, number=2)
    emoji_id = proto.Field(proto.INT64, number=3)
    emoji_content = proto.Field(Text, number=4)
    default_content = proto.Field(proto.STRING, number=5)
    background_image = proto.Field(Image, number=6)
    from_intercom = proto.Field(proto.BOOL, number=7)
    intercom_hide_user_card = proto.Field(proto.BOOL, number=8)


class RoomUserSeqMessageContributor(proto.Message):
    """在线统计中的单个用户贡献者。"""
    score = proto.Field(proto.UINT64, number=1)
    user = proto.Field(User, number=2)
    rank = proto.Field(proto.UINT64, number=3)
    delta = proto.Field(proto.UINT64, number=4)
    is_hidden = proto.Field(proto.BOOL, number=5)
    score_description = proto.Field(proto.STRING, number=6)
    exactly_score = proto.Field(proto.STRING, number=7)


class RoomUserSeqMessage(proto.Message):
    """实时在线人数统计消息。"""
    common = proto.Field(Common, number=1)
    ranks_list = proto.RepeatedField(RoomUserSeqMessageContributor, number=2)
    total = proto.Field(proto.INT64, number=3)
    pop_str = proto.Field(proto.STRING, number=4)
    seats_list = proto.RepeatedField(RoomUserSeqMessageContributor, number=5)
    popularity = proto.Field(proto.INT64, number=6)
    total_user = proto.Field(proto.INT64, number=7)
    total_user_str = proto.Field(proto.STRING, number=8)
    total_str = proto.Field(proto.STRING, number=9)
    online_user_for_anchor = proto.Field(proto.STRING, number=10)
    total_pv_for_anchor = proto.Field(proto.STRING, number=11)
    up_right_stats_str = proto.Field(proto.STRING, number=12)
    up_right_stats_str_complete = proto.Field(proto.STRING, number=13)


class RoomStatsMessage(proto.Message):
    """直播累计统计消息（观看人次等）。"""
    common = proto.Field(Common, number=1)
    display_short = proto.Field(proto.STRING, number=2)
    display_middle = proto.Field(proto.STRING, number=3)
    display_long = proto.Field(proto.STRING, number=4)
    display_value = proto.Field(proto.INT64, number=5)
    display_version = proto.Field(proto.INT64, number=6)
    incremental = proto.Field(proto.BOOL, number=7)
    is_hidden = proto.Field(proto.BOOL, number=8)
    total = proto.Field(proto.INT64, number=9)
    display_type = proto.Field(proto.INT64, number=10)


class GiftStruct(proto.Message):
    """礼物结构体（礼物类型、名称、抖币价格等）。"""
    image = proto.Field(Image, number=1)
    describe = proto.Field(proto.STRING, number=2)
    notify = proto.Field(proto.BOOL, number=3)
    duration = proto.Field(proto.UINT64, number=4)
    id = proto.Field(proto.UINT64, number=5)
    for_linkmic = proto.Field(proto.BOOL, number=7)
    doodle = proto.Field(proto.BOOL, number=8)
    for_fansclub = proto.Field(proto.BOOL, number=9)
    combo = proto.Field(proto.BOOL, number=10)
    type = proto.Field(proto.UINT32, number=11)
    diamond_count = proto.Field(proto.UINT32, number=12)
    is_displayed_on_panel = proto.Field(proto.BOOL, number=13)
    primary_effect_id = proto.Field(proto.UINT64, number=14)
    gift_label_icon = proto.Field(Image, number=15)
    name = proto.Field(proto.STRING, number=16)
    region = proto.Field(proto.STRING, number=17)
    manual = proto.Field(proto.STRING, number=18)
    for_custom = proto.Field(proto.BOOL, number=19)
    icon = proto.Field(Image, number=21)
    action_type = proto.Field(proto.UINT32, number=22)


class GiftIMPriority(proto.Message):
    """礼物 IM 优先级。"""
    queue_sizes_list = proto.RepeatedField(proto.UINT64, number=1)
    self_queue_priority = proto.Field(proto.UINT64, number=2)
    priority = proto.Field(proto.UINT64, number=3)


class TextEffectDetail(proto.Message):
    """文字特效详情（阴影、描边、位置等）。"""
    text = proto.Field(Text, number=1)
    text_font_size = proto.Field(proto.UINT32, number=2)
    background = proto.Field(Image, number=3)
    start = proto.Field(proto.UINT32, number=4)
    duration = proto.Field(proto.UINT32, number=5)
    x = proto.Field(proto.UINT32, number=6)
    y = proto.Field(proto.UINT32, number=7)
    width = proto.Field(proto.UINT32, number=8)
    height = proto.Field(proto.UINT32, number=9)
    shadow_dx = proto.Field(proto.UINT32, number=10)
    shadow_dy = proto.Field(proto.UINT32, number=11)
    shadow_radius = proto.Field(proto.UINT32, number=12)
    shadow_color = proto.Field(proto.STRING, number=13)
    stroke_color = proto.Field(proto.STRING, number=14)
    stroke_width = proto.Field(proto.UINT32, number=15)


class TextEffect(proto.Message):
    """文字特效（横屏/竖屏两套配置）。"""
    portrait = proto.Field(TextEffectDetail, number=1)
    landscape = proto.Field(TextEffectDetail, number=2)


class GiftMessage(proto.Message):
    """礼物赠送消息。

    关键字段：gift（礼物结构体）、combo_count / total_count（数量）、
    diamond_count × count = 抖币总额。
    """
    common = proto.Field(Common, number=1)
    gift_id = proto.Field(proto.UINT64, number=2)
    fan_ticket_count = proto.Field(proto.UINT64, number=3)
    group_count = proto.Field(proto.UINT64, number=4)
    repeat_count = proto.Field(proto.UINT64, number=5)
    combo_count = proto.Field(proto.UINT64, number=6)
    user = proto.Field(User, number=7)
    to_user = proto.Field(User, number=8)
    repeat_end = proto.Field(proto.UINT32, number=9)
    text_effect = proto.Field(TextEffect, number=10)
    group_id = proto.Field(proto.UINT64, number=11)
    income_taskgifts = proto.Field(proto.UINT64, number=12)
    room_fan_ticket_count = proto.Field(proto.UINT64, number=13)
    priority = proto.Field(GiftIMPriority, number=14)
    gift = proto.Field(GiftStruct, number=15)
    log_id = proto.Field(proto.STRING, number=16)
    send_type = proto.Field(proto.UINT64, number=17)
    public_area_common = proto.Field(PublicAreaCommon, number=18)
    tray_display_text = proto.Field(Text, number=19)
    banned_display_effects = proto.Field(proto.UINT64, number=20)
    display_for_self = proto.Field(proto.BOOL, number=25)
    interact_gift_info = proto.Field(proto.STRING, number=26)
    diy_item_info = proto.Field(proto.STRING, number=27)
    min_asset_set_list = proto.RepeatedField(proto.UINT64, number=28)
    total_count = proto.Field(proto.UINT64, number=29)
    client_gift_source = proto.Field(proto.UINT32, number=30)
    to_user_ids_list = proto.RepeatedField(proto.UINT64, number=32)
    send_time = proto.Field(proto.UINT64, number=33)
    force_display_effects = proto.Field(proto.UINT64, number=34)
    trace_id = proto.Field(proto.STRING, number=35)
    effect_display_ts = proto.Field(proto.UINT64, number=36)


class MemberMessage(proto.Message):
    """进场消息（用户进入直播间）。"""
    common = proto.Field(Common, number=1)
    user = proto.Field(User, number=2)
    member_count = proto.Field(proto.UINT64, number=3)
    operator = proto.Field(User, number=4)
    is_set_to_admin = proto.Field(proto.BOOL, number=5)
    is_top_user = proto.Field(proto.BOOL, number=6)
    rank_score = proto.Field(proto.UINT64, number=7)
    top_user_no = proto.Field(proto.UINT64, number=8)
    enter_type = proto.Field(proto.UINT64, number=9)
    action = proto.Field(proto.UINT64, number=10)
    action_description = proto.Field(proto.STRING, number=11)
    user_id = proto.Field(proto.UINT64, number=12)
    effect_config = proto.Field(EffectConfig, number=13)
    pop_str = proto.Field(proto.STRING, number=14)
    enter_effect_config = proto.Field(EffectConfig, number=15)
    background_image = proto.Field(Image, number=16)
    background_image_v2 = proto.Field(Image, number=17)
    anchor_display_text = proto.Field(Text, number=18)
    public_area_common = proto.Field(PublicAreaCommon, number=19)
    user_enter_tip_type = proto.Field(proto.UINT64, number=20)
    anchor_enter_tip_type = proto.Field(proto.UINT64, number=21)


class LikeMessage(proto.Message):
    """点赞消息（双击点赞）。"""
    common = proto.Field(Common, number=1)
    count = proto.Field(proto.UINT64, number=2)
    total = proto.Field(proto.UINT64, number=3)
    color = proto.Field(proto.UINT64, number=4)
    user = proto.Field(User, number=5)
    icon = proto.Field(proto.STRING, number=6)
    double_like_detail = proto.Field("DoubleLikeDetail", number=7)
    display_control_info = proto.Field("DisplayControlInfo", number=8)
    linkmic_guest_uid = proto.Field(proto.UINT64, number=9)
    scene = proto.Field(proto.STRING, number=10)
    pico_display_info = proto.Field("PicoDisplayInfo", number=11)


class DoubleLikeDetail(proto.Message):
    """双击点赞详情。"""
    double_flag = proto.Field(proto.BOOL, number=1)
    seq_id = proto.Field(proto.UINT32, number=2)
    renewals_num = proto.Field(proto.UINT32, number=3)
    triggers_num = proto.Field(proto.UINT32, number=4)


class DisplayControlInfo(proto.Message):
    """显示控制信息。"""
    show_text = proto.Field(proto.BOOL, number=1)
    show_icons = proto.Field(proto.BOOL, number=2)


class PicoDisplayInfo(proto.Message):
    """点赞累计显示信息。"""
    combo_sum_count = proto.Field(proto.UINT64, number=1)
    emoji = proto.Field(proto.STRING, number=2)
    emoji_icon = proto.Field(Image, number=3)
    emoji_text = proto.Field(proto.STRING, number=4)


class SocialMessage(proto.Message):
    """关注/分享消息。action=1 为关注，action=2 为分享。"""
    common = proto.Field(Common, number=1)
    user = proto.Field(User, number=2)
    share_type = proto.Field(proto.UINT64, number=3)
    action = proto.Field(proto.UINT64, number=4)
    share_target = proto.Field(proto.STRING, number=5)
    follow_count = proto.Field(proto.UINT64, number=6)
    public_area_common = proto.Field(PublicAreaCommon, number=7)


class FansclubMessage(proto.Message):
    """粉丝团消息（加入/升级）。"""
    common_info = proto.Field(Common, number=1)
    type = proto.Field(proto.INT32, number=2)
    content = proto.Field(proto.STRING, number=3)
    user = proto.Field(User, number=4)


class CommonTextMessage(proto.Message):
    """通用文本消息。"""
    common = proto.Field(Common, number=1)
    user = proto.Field(User, number=2)
    scene = proto.Field(proto.STRING, number=3)


class UpdateFanTicketMessage(proto.Message):
    """粉丝票更新消息。"""
    common = proto.Field(Common, number=1)
    room_fan_ticket_count_text = proto.Field(proto.STRING, number=2)
    room_fan_ticket_count = proto.Field(proto.UINT64, number=3)
    force_update = proto.Field(proto.BOOL, number=4)


class ControlMessage(proto.Message):
    """直播控制消息。status=1 开始，2 暂停，3 已结束。"""
    common = proto.Field(Common, number=1)
    status = proto.Field(proto.INT32, number=2)


class RoomRankMessageRoomRank(proto.Message):
    """排行榜中的单个用户排名。"""
    user = proto.Field(User, number=1)
    score_str = proto.Field(proto.STRING, number=2)
    profile_hidden = proto.Field(proto.BOOL, number=3)


class RoomRankMessage(proto.Message):
    """积分排行榜消息。"""
    common = proto.Field(Common, number=1)
    ranks_list = proto.RepeatedField(RoomRankMessageRoomRank, number=2)


class RoomMessage(proto.Message):
    """直播间公告消息（置顶、场景等）。"""
    common = proto.Field(Common, number=1)
    content = proto.Field(proto.STRING, number=2)
    supprot_landscape = proto.Field(proto.BOOL, number=3)
    roommessagetype = proto.Field(proto.INT32, number=4)
    system_top_msg = proto.Field(proto.BOOL, number=5)
    forced_guarantee = proto.Field(proto.BOOL, number=6)
    biz_scene = proto.Field(proto.STRING, number=20)
    buried_point_map = proto.MapField(proto.STRING, proto.STRING, number=30)


class RoomStreamAdaptationMessage(proto.Message):
    """流配置消息（自适应分辨率）。"""
    common = proto.Field(Common, number=1)
    adaptation_type = proto.Field(proto.INT32, number=2)
    adaptation_height_ratio = proto.Field(proto.FLOAT, number=3)
    adaptation_body_center_ratio = proto.Field(proto.FLOAT, number=4)


# ── 业务层：电商/购物消息 ─────────────────────────

class LiveShoppingMessage(proto.Message):
    """直播购物消息。"""
    common = proto.Field(Common, number=1)
    msg_type = proto.Field(proto.INT32, number=2)
    promotion_id = proto.Field(proto.INT64, number=4)


class ProductInfo(proto.Message):
    """商品信息。"""
    promotion_id = proto.Field(proto.INT64, number=1)
    index = proto.Field(proto.INT32, number=2)
    target_flash_uids_list = proto.RepeatedField(proto.INT64, number=3)
    explain_type = proto.Field(proto.INT64, number=4)


class CategoryInfo(proto.Message):
    """商品分类信息。"""
    id = proto.Field(proto.INT32, number=1)
    name = proto.Field(proto.STRING, number=2)
    promotion_ids_list = proto.RepeatedField(proto.INT64, number=3)
    type = proto.Field(proto.STRING, number=4)
    unique_index = proto.Field(proto.STRING, number=5)


class ProductChangeMessage(proto.Message):
    """商品变更消息。"""
    common = proto.Field(Common, number=1)
    update_timestamp = proto.Field(proto.INT64, number=2)
    update_toast = proto.Field(proto.STRING, number=3)
    update_product_info_list = proto.RepeatedField(ProductInfo, number=4)
    total = proto.Field(proto.INT64, number=5)
    update_category_info_list = proto.RepeatedField(CategoryInfo, number=8)


# ── 业务层：赛事/剧集消息 ─────────────────────────

class Against(proto.Message):
    """赛事对阵信息（比分等）。"""
    left_name = proto.Field(proto.STRING, number=1)
    left_logo = proto.Field(Image, number=2)
    left_goal = proto.Field(proto.STRING, number=3)
    right_name = proto.Field(proto.STRING, number=6)
    right_logo = proto.Field(Image, number=7)
    right_goal = proto.Field(proto.STRING, number=8)
    timestamp = proto.Field(proto.UINT64, number=11)
    version = proto.Field(proto.UINT64, number=12)
    left_team_id = proto.Field(proto.UINT64, number=13)
    right_team_id = proto.Field(proto.UINT64, number=14)
    diff_sei2abs_second = proto.Field(proto.UINT64, number=15)
    final_goal_stage = proto.Field(proto.UINT32, number=16)
    current_goal_stage = proto.Field(proto.UINT32, number=17)
    left_score_addition = proto.Field(proto.UINT32, number=18)
    right_score_addition = proto.Field(proto.UINT32, number=19)
    left_goal_int = proto.Field(proto.UINT64, number=20)
    right_goal_int = proto.Field(proto.UINT64, number=21)


class MatchAgainstScoreMessage(proto.Message):
    """赛事比分消息。"""
    common = proto.Field(Common, number=1)
    against = proto.Field(Against, number=2)
    match_status = proto.Field(proto.UINT32, number=3)
    display_status = proto.Field(proto.UINT32, number=4)


class EpisodeChatMessage(proto.Message):
    """剧集聊天消息（可能已废弃，common 类型为 Message 而非 Common）。"""
    common = proto.Field(Message, number=1)
    user = proto.Field(User, number=2)
    content = proto.Field(proto.STRING, number=3)
    visible_to_sende = proto.Field(proto.BOOL, number=4)
    gift_image = proto.Field(Image, number=7)
    agree_msg_id = proto.Field(proto.UINT64, number=8)
    color_value_list = proto.RepeatedField(proto.STRING, number=9)


# ── 辅助层：逆向所得含义不明的加密/握手字段 ───────
# 这些类来自协议逆向，字段名被压缩，具体含义不确定，
# 仅用于 protobuf 反序列化，不直接参与业务逻辑。

class Kk(proto.Message):
    """含义不明的加密辅助字段。"""
    k = proto.Field(proto.UINT32, number=14)


class ExtList(proto.Message):
    """扩展键值对。"""
    key = proto.Field(proto.STRING, number=1)
    value = proto.Field(proto.STRING, number=2)


class RspF(proto.Message):
    """握手响应的未知子字段（来自逆向，含义不明）。"""
    q1 = proto.Field(proto.UINT64, number=1)
    q3 = proto.Field(proto.UINT64, number=3)
    q4 = proto.Field(proto.STRING, number=4)
    q5 = proto.Field(proto.UINT64, number=5)


class Rsp(proto.Message):
    """握手响应体（来自逆向，字段含义不明）。"""
    a = proto.Field(proto.INT32, number=1)
    b = proto.Field(proto.INT32, number=2)
    c = proto.Field(proto.INT32, number=3)
    d = proto.Field(proto.STRING, number=4)
    e = proto.Field(proto.INT32, number=5)
    f = proto.Field(RspF, number=6)
    g = proto.Field(proto.STRING, number=7)
    h = proto.Field(proto.UINT64, number=10)
    i = proto.Field(proto.UINT64, number=11)
    j = proto.Field(proto.UINT64, number=13)


class SendMessageBody(proto.Message):
    """消息发送体。"""
    conversation_id = proto.Field(proto.STRING, number=1)
    conversation_type = proto.Field(proto.UINT32, number=2)
    conversation_short_id = proto.Field(proto.UINT64, number=3)
    content = proto.Field(proto.STRING, number=4)
    ext = proto.RepeatedField(ExtList, number=5)
    message_type = proto.Field(proto.UINT32, number=6)
    ticket = proto.Field(proto.STRING, number=7)
    client_message_id = proto.Field(proto.STRING, number=8)


class PreMessage(proto.Message):
    """消息预处理结构。"""
    cmd = proto.Field(proto.UINT32, number=1)
    sequence_id = proto.Field(proto.UINT32, number=2)
    sdk_version = proto.Field(proto.STRING, number=3)
    token = proto.Field(proto.STRING, number=4)
    refer = proto.Field(proto.UINT32, number=5)
    inbox_type = proto.Field(proto.UINT32, number=6)
    build_number = proto.Field(proto.STRING, number=7)
    send_message_body = proto.Field(SendMessageBody, number=8)
    aa = proto.Field(proto.STRING, number=9)
    device_platform = proto.Field(proto.STRING, number=11)
    headers = proto.RepeatedField(HeadersList, number=15)
    auth_type = proto.Field(proto.UINT32, number=18)
    biz = proto.Field(proto.STRING, number=21)
    access = proto.Field(proto.STRING, number=22)


def parse_proto(cls, data):
    """将 protobuf 序列化字节反序列化为 proto-plus 对象。

    Args:
        cls: proto-plus 消息类（如 ChatMessage、PushFrame）。
        data: protobuf 序列化的二进制数据。

    Returns:
        对应类的 proto-plus 实例。
    """
    pb = cls.meta.pb()
    pb.ParseFromString(data)
    return cls.wrap(pb)
