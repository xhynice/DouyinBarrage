# 弹幕数据构建脚本

将原始 CSV/JSONL 数据转换为前端可用的 JSON 格式。

## 使用方式

```bash
python scripts/build_barrage.py
```

## 输入结构

```
data/
└── {live_id}/
    ├── meta.json              # 直播间元数据
    ├── avatar.jpg             # 主播头像
    ├── cover.jpg              # 直播间封面
    └── {YYYYMMDD}_{HHMM}_{room_id}/
        ├── chat.csv
        ├── gift.csv
        ├── like.csv
        └── ...
```

## 输出结构

```
docs/data/barrage/
├── index.json                    # 全局索引
└── {live_id}/
    ├── index.json                # 直播间索引
    ├── avatar.jpg                # 主播头像
    ├── cover.jpg                 # 直播间封面
    └── {session_id}/
        ├── meta.json             # 会话元数据
        ├── chat.jsonl            # 弹幕数据
        ├── gift.jsonl            # 礼物数据
        └── ...
```

## 支持的消息类型

| 类型 | 文件名 | 说明 |
|------|--------|------|
| chat | chat.csv | 聊天弹幕 |
| gift | gift.csv | 礼物 |
| lucky_bag | lucky_bag.csv | 福袋 |
| member | member.csv | 进场 |
| social | social.csv | 关注/分享 |
| like | like.csv | 点赞 |
| fansclub | fansclub.csv | 粉丝团 |
| stats | stats.csv | 统计 |
| roomstats | roomstats.csv | 房间统计 |
| room | room.csv | 房间信息 |
| rank | rank.csv | 排行榜 |
| control | control.csv | 控制消息 |
| emoji | emoji.csv | 表情 |

## 输出文件说明

### index.json（全局索引）

```json
{
  "live_rooms": [
    {
      "live_id": "126833924894",
      "anchor_name": "主播名",
      "session_count": 12,
      "latest_session": "20260504_2055_7635973128329415487",
      "total_stats": {"chat": 1234, "gift": 56}
    }
  ],
  "type_config": {...},
  "generated_at": "2026-05-05 12:00:00"
}
```

### index.json（直播间索引）

```json
{
  "live_id": "126833924894",
  "anchor_name": "主播名",
  "anchor_avatar": "...",
  "room_title": "...",
  "sessions": [...]
}
```

### meta.json（会话元数据）

```json
{
  "session_id": "20260504_2055_7635973128329415487",
  "live_id": "126833924894",
  "available_types": ["chat", "gift", "like"],
  "stats": {"chat": 100, "gift": 20, "like": 50},
  "total": 170,
  "rankings": {...},
  "gift_diamond": 1500,
  "total_pv": 10000
}
```

## 排行榜计算

脚本自动计算以下排行榜：

| 类型 | 说明 |
|------|------|
| chat | 发言数排行、@次数排行 |
| gift | 礼物抖币排行、最大单次礼物 |
| like | 点赞数排行 |
| lucky_bag | 福袋参与次数排行 |
