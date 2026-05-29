# 弹幕数据构建脚本

将原始 SQLite/CSV 数据转换为前端可用的 JSON 格式。

仅使用 CSV 会话目录构建（场次边界准确），旧格式目录名自动迁移为 `YYYYMMDD_HHMM` 格式。

## 使用方式

```bash
python docs/build_barrage.py
```

## 输入结构

```
data/
└── {主播名}/
    ├── meta.json              # 直播间元数据
    ├── avatar.jpg             # 主播头像
    ├── cover.jpg              # 直播间封面
    └── {YYYYMMDD_HHMM}/       # CSV 会话目录（旧格式自动迁移）
        ├── chat.csv
        ├── gift.csv
        ├── like.csv
        └── ...
```

- 遍历 CSV 会话目录，逐个转换
- 旧格式目录名（如 `20260424_1203_7632171016466238259`、`2026-05-29_11-48-32`）自动迁移为 `YYYYMMDD_HHMM`

## 输出结构

```
docs/data/barrage/
├── index.json                    # 全局索引
└── {主播名}/
    ├── index.json                # 直播间索引
    ├── avatar.jpg                # 主播头像
    └── {YYYYMMDD_HHMM}/          # 会话目录名
        ├── meta.json             # 会话元数据
        ├── chat.jsonl            # 弹幕数据
        ├── gift.jsonl            # 礼物数据
        └── ...
```

## 支持的消息类型

| 类型 | 表名 / 文件名 | 说明 |
|------|--------------|------|
| chat | chat | 聊天弹幕 |
| gift | gift | 礼物 |
| lucky_bag | lucky_bag | 福袋 |
| member | member | 进场 |
| social | social | 关注/分享 |
| like | like | 点赞 |
| fansclub | fansclub | 粉丝团 |
| stats | stats | 统计 |
| roomstats | roomstats | 房间统计 |
| room | room | 房间信息 |
| rank | rank | 排行榜 |
| control | control | 控制消息 |
| emoji | emoji | 表情 |

## 输出文件说明

### index.json（全局索引）

```json
{
  "live_rooms": [
    {
      "anchor_name": "主播名",
      "session_count": 12,
      "latest_session": "2026-05-04",
      "total_stats": {"chat": 1234, "gift": 56}
    }
  ],
  "type_config": {...},
  "generated_at": "2026-05-29 12:00:00"
}
```

### index.json（直播间索引）

```json
{
  "anchor_name": "主播名",
  "anchor_avatar": "...",
  "room_title": "...",
  "sessions": [...]
}
```

### meta.json（会话元数据）

```json
{
  "session_id": "2026-05-04",
  "anchor_name": "主播名",
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

## 历史数据说明

### 跨夜时间修正

2026-05-29 之前的采集数据存在跨夜时间字段错误。跨午夜会话（如 21:03 开播至次日 00:30 下播）的 CSV `time` 字段，次日凌晨的时间仍标记为当天日期。

例如 `20260507_2302/control.csv`：
- 修正前：`2026-05-07 00:31:59`（错误，应为次日）
- 修正后：`2026-05-08 00:31:59`（正确）

此问题已通过批量修正脚本修复，所有历史数据的日期已正确。

### 会话目录格式迁移

旧格式目录名会自动迁移为 `YYYYMMDD_HHMM`：

| 旧格式 | 迁移后 |
|--------|--------|
| `20260424_1203_7632171016466238259` | `20260424_1203` |
| `2026-05-29_11-48-32` | `20260529_1148` |

构建脚本会自动检测并迁移，原目录中的文件移至新目录后删除原目录。
