# DouyinBarrage

> 抖音直播间弹幕数据实时采集器 — WebSocket 长连接，13 种消息类型，CSV / SQLite 双格式输出，集成直播录制。

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![Node.js](https://img.shields.io/badge/Node.js-20+-green.svg)
![License](https://img.shields.io/badge/License-MIT-lightgrey.svg)

## 运行方法

### 1. 安装依赖

```bash
# Python 依赖
pip install -r requirements.txt

# Node.js 依赖（签名脚本）
npm install crypto-js

# 系统依赖
# FFmpeg（直播录制需要，不录制可跳过）
# Ubuntu/Debian: sudo apt install ffmpeg
# macOS: brew install ffmpeg
```

### 2. 配置 Cookie（可选）

未登录时部分消息（如礼物详情）可能受限。提供登录 Cookie 可获取完整数据。

```bash
# 复制样本文件
cp cookie.example.txt cookie.txt
```

**获取 Cookie：**

1. 浏览器登录 [抖音](https://www.douyin.com)
2. 按 `F12` 打开开发者工具 → Application → Cookies → `douyin.com`
3. 全选复制所有 Cookie，粘贴到 `cookie.txt`

> **不配置 Cookie 的影响：** 游客模式下可正常采集弹幕、点赞、进场等基础消息，但礼物详情、用户等级等信息可能不完整。

### 3. 配置房间

编辑 `rooms.txt`，每行一个房间：

```csv
126833924894,张君雅
235371120297,才圆圆
#662819707065,不启用的房间
```

- 格式：`id,name`（逗号分隔）
- `#` 开头 = 不启用
- 空行自动跳过
- 主播名可选，首次连接时自动获取并更新

### 4. 运行

```bash
# 交互式选择房间
python main.py

# 直接指定直播间 ID
python main.py 536863152858

# 启用录制
python main.py 536863152858 --record

# 采集全部房间
python main.py --all
```

## 命令行参数

| 参数 | 说明 |
|------|------|
| `live_id` | 直播间 ID（不提供则交互式选择） |
| `--log-level {DEBUG,INFO,WARNING,ERROR,NONE}` | 覆盖日志级别 |
| `--live-stop` | 直播结束后停止退出（默认跟随配置文件） |
| `--live-wait` | 直播结束后等待重开播（默认跟随配置文件） |
| `--record` | 启用直播流录制（覆盖配置文件中的 `record.enabled`） |
| `--all` | 采集 `rooms.txt` 中全部未注释的房间（跳过交互选择） |

**示例：**

```bash
# 调试模式
python main.py 536863152858 --log-level DEBUG

# 录制 + 直播结束退出
python main.py 536863152858 --record --live-stop

# 全部房间 + 录制
python main.py --all --record
```

## 配置文件

编辑 `config.yaml`：

```yaml
# ==================== 输出配置 ====================
output_dir: data                 # 统一输出目录（弹幕数据 + 录制视频 + 元数据）

# ==================== 等待开播配置 ====================
live_stop: true                  # 直播结束后是否停止退出: true=结束退出 / false=等待重开播
live_check_interval: 160         # 未开播 HTTP 轮询间隔（秒）

# ==================== 消息类型开关 ====================
output:
  chat: true                     # 弹幕
  lucky_bag: true                # 福袋口令
  gift: true                     # 礼物
  like: true                     # 点赞
  member: false                  # 进场
  social: true                   # 关注/分享
  rank: false                    # 排行榜
  stats: true                    # 统计
  fansclub: false                # 粉丝团
  emoji: false                   # 表情
  room: false                    # 直播间公告
  roomstats: false               # 直播统计
  control: true                  # 直播状态

# ==================== 弹幕配置 ====================
barrage:
  gift_combo_final: true         # 礼物连击过滤: true=只记录连击最终值(x520) / false=记录每次递增
  csv: true                      # CSV 输出
  sqlite: false                  # SQLite 输出

# ==================== 录制配置 ====================
record:
  enabled: false                 # 是否同时录制直播流
  format: ts                     # 封装格式: ts / flv / mp4
  quality: 原画                  # 画质: 原画/超清/高清/标清/省流
  segment_time: 0                # 分段时长（秒），0=不分段，建议 3600（1小时）
  segment_size: 0                # 分段文件大小（MB），0=不限制，建议 2048（2GB）
  auto_convert: true             # 录制结束后自动 ts→mp4 转码（需 ffmpeg）

# ==================== API 配置 ====================
api:
  enabled: false                 # 是否启用 HTTP API 服务
  host: 0.0.0.0                  # 监听地址
  port: 8088                     # 监听端口
```

## 更新记录

详见 [CHANGELOG.md](CHANGELOG.md)

## 免责声明

本项目仅供学习研究使用，请勿用于商业用途或违反平台规则的行为。采集的数据仅用于技术研究，请勿传播或用于非法目的。

## 致谢

- [DouYin_Spider](https://github.com/cv-cat/DouYin_Spider) — 签名脚本参考
- [DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher) — 弹幕爬取参考
- [DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) — 录制架构与多 API 降级参考
