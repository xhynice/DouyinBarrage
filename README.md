# douyin-barrage

> 抖音直播间弹幕数据实时采集器 — WebSocket 长连接，13 种消息类型，CSV/JSONL 双格式输出。

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![Node.js](https://img.shields.io/badge/Node.js-20+-green.svg)
![License](https://img.shields.io/badge/License-MIT-lightgrey.svg)

## 功能特性

- **实时采集** — 基于 WebSocket 长连接，毫秒级获取直播间弹幕数据
- **13 种消息** — 弹幕、礼物、点赞、关注、进场、粉丝团、福袋、表情、统计等
- **双格式输出** — CSV（UTF-8 BOM，Excel 直接打开）和 JSONL
- **登录态支持** — 支持 Cookie 登录，获取完整礼物等数据
- **多房间并发** — 同时采集多个直播间，状态面板轮显
- **等待开播** — 下播后自动监控，开播立即重新采集
- **弱网容错** — 自动重连、看门狗检测静默断连、gzip 损坏包跳过
- **假死检测** — 业务消息看门狗，检测"有数据但无弹幕"的假活状态
- **灵活配置** — 每种消息类型独立开关，输出格式可选

## 安装

### 环境要求

- Python 3.11+
- Node.js v20+（执行签名脚本）

### 安装步骤

```bash
git clone https://github.com/xhynice/DouyinBarrage
cd DouyinBarrage
pip install -r requirements.txt
```

### 运行

```bash
# 交互式选择房间（从 config.yaml 读取）
python main.py

# 直接指定直播间 ID
python main.py 536863152858

# 调试模式
python main.py 536863152858 --log-level DEBUG

# 直播结束后退出
python main.py 536863152858 --live-stop

# 直播结束后等待重开播
python main.py 536863152858 --live-wait

# 停止采集
Ctrl+C
```

### 命令行参数

| 参数 | 说明 |
|------|------|
| `live_id` | 直播间 ID（可选，不提供则交互式输入） |
| `--log-level` | 覆盖日志级别：`DEBUG` / `BARRAGE` / `INFO` / `WARNING` / `ERROR` / `NONE` |
| `--live-stop` | 直播结束后停止退出（默认跟随配置文件） |
| `--live-wait` | 直播结束后等待重开播（默认跟随配置文件） |

## 配置说明

### 房间配置

房间列表通过 `rooms.txt` 文件管理，每行一个房间。

#### 文件格式

```
126833924894,张君雅
235371120297,才圆圆
#662819707065,不启用的房间
```

- 格式：`id,name`（逗号分隔）
- `#` 开头 = 不启用
- 空行自动跳过
- 主播名可选，首次连接时自动获取并更新

### Cookie 配置

未登录时部分消息（如礼物详情）可能受限。提供登录 Cookie 可获取完整数据。

#### 快速配置

1. 复制样本文件：
   ```bash
   cp cookie.example.txt cookie.txt
   ```

2. 浏览器登录 [抖音](https://www.douyin.com)

3. 按 `F12` 打开开发者工具 → Application → Cookies → `douyin.com`

4. 全选复制所有 Cookie，粘贴到 `cookie.txt`

#### 支持格式

**格式一** — 浏览器导出（推荐）：

```
name1=value1; name2=value2; name3=value3
```

**格式二** — 每行一个：

```
name1=value1
name2=value2
```

**格式三** — Netscape cookie jar：

```
.douyin.com	TRUE	/	FALSE	0	sessionid	abc123
```

#### 关键字段

| 字段 | 说明 |
|------|------|
| `sessionid` | 会话标识，必需 |
| `ttwid` | 设备标识，自动获取 |
| `s_v_web_id` | 验证 ID |

> ⚠️ Cookie 包含敏感信息，`cookie.txt` 已加入 `.gitignore`，请勿提交到版本控制。

### 配置文件

编辑 `config.yaml`：

```yaml
log_level: INFO              # 日志级别: DEBUG / BARRAGE / INFO / WARNING / ERROR / NONE
cookie_file: cookie.txt      # Cookie 文件路径

live_stop: false             # 直播结束后是否停止退出: true=结束退出, false=等待重开播
live_check_interval: 120     # 未开播 HTTP 轮询间隔（秒）

# ==================== 输出配置 ====================
output:
  chat: true                 # 弹幕
  gift: true                 # 礼物
  like: true                 # 点赞
  member: false              # 进场
  social: true               # 关注/分享
  stats: true                # 统计
  lucky_bag: true            # 福袋口令
  rank: false                # 排行榜
  fansclub: false            # 粉丝团
  emoji: false               # 表情
  room: false                # 直播间公告
  roomstats: false           # 直播统计
  control: true              # 直播状态
  file_format: csv           # 输出格式: csv / json / both / none
  file_dir: data             # 输出目录

# ==================== 网络配置 ====================
network:
  http_timeout: 15           # HTTP 超时（秒），超时后自动 ×1.5，封顶 60s，最多重试 3 次
  ws_connect_timeout: 30     # WebSocket 底层 socket 超时（秒）
  silence_timeout: 60        # 看门狗静默阈值（秒），超过则判定断连并重连
  heartbeat_interval: 10     # 心跳间隔（秒）
  rcvbuf_kb: 256             # 接收缓冲区（KB），高流量直播间建议 512
  proxy: null                # 代理地址，如 {"http": "http://127.0.0.1:8080"}

# ==================== 重连配置 ====================
max_reconnects: 3            # 最大重连次数（0=无限）
reconnect_base_delay: 2      # 重连基础延迟（秒），指数退避：2s → 4s → 8s → ...
reconnect_max_delay: 120     # 最大重连延迟（秒），退避封顶

# ==================== 统计配置 ====================
stats_interval: 300          # 吞吐统计打印间隔（秒）
```

### 日志级别

| 级别 | 说明 |
|------|------|
| `DEBUG` | 调试信息，输出所有详细日志 |
| `BARRAGE` | 弹幕级别，包含输出弹幕消息 |
| `INFO` | 常规信息，不包含弹幕信息仅输出运行状态和关键事件 |
| `WARNING` | 警告信息，仅输出警告和错误 |
| `ERROR` | 错误信息，仅输出错误 |
| `NONE` | 关闭日志，数据文件照常写入 |

### 输出格式

| 值 | 说明 |
|----|------|
| `none` | 仅输出到日志 |
| `csv` | 按消息类型分 CSV 文件 |
| `json` | 按消息类型分 JSONL 文件 |
| `both` | CSV + JSONL 同时输出 |

### 弱网环境推荐配置

```yaml
network:
  http_timeout: 30
  ws_connect_timeout: 60
  silence_timeout: 180
  heartbeat_interval: 20
  rcvbuf_kb: 512
```

### 消息类型

| 类型 | 说明 | 日志标识 | 配置键 |
|------|------|----------|--------|
| 聊天 | 用户文字弹幕 | `[聊天]` | `chat` |
| 福袋口令 | 福袋活动口令 | `[福袋口令]` | `lucky_bag` |
| 进场 | 用户进入直播间 | `[进场]` | `member` |
| 点赞 | 用户双击点赞 | `[点赞]` | `like` |
| 关注 | 用户关注主播 | `[关注/分享]` | `social` |
| 礼物 | 用户赠送礼物（含抖币计算） | `[礼物]` | `gift` |
| 粉丝团 | 加入/升级粉丝团 | `[粉丝团]` | `fansclub` |
| 表情 | 用户发送表情包 | `[表情]` | `emoji` |
| 统计 | 实时在线人数 | `[统计]` | `stats` |
| 直播统计 | 累计观看等 | `[直播统计]` | `roomstats` |
| 直播间 | 置顶公告等 | `[直播间]` | `room` |
| 排行榜 | 积分排行榜 | `[排行榜]` | `rank` |
| 直播状态 | 开始/暂停/结束 | `[直播状态]` | `control` |

## 机制说明

### 运行机制

```
1. 加载 Cookie    → 从 cookie.txt 加载登录态，否则游客模式
2. 获取 ttwid     → HTTP 请求 live.douyin.com 获取（懒加载，首次访问触发）
3. 获取 roomId    → 调用 enter_room_api 解析直播间信息
4. 生成签名       → 13 参数拼接 → MD5 → Node.js 执行 sign.js → X-Bogus
5. WebSocket 连接 → 携带登录 Cookie + 签名建立长连接
6. 消息处理       → PushFrame → gzip 解压 → Response → 按类型分发
7. 数据输出       → 异步日志 + CSV/JSONL 批量写入（2s 刷新）
```

### 线程模型

```
主线程 (_connectWebSocket)
│   WebSocket 连接循环（含重连逻辑）
│   run_forever() 阻塞在此
│
├── heartbeat (daemon)    每 10s 发送心跳包
├── watchdog  (daemon)    每 10s 检查：
│   ├── 连接建立超时（60s 未建立连接）
│   ├── 数据静默超时（60s 无任何数据）
│   └── 业务消息超时（60s 有数据但无业务消息）
├── stats     (daemon)    每 300s 打印吞吐统计
└── monitor   (daemon)    等待开播模式下的 HTTP 轮询
```

### 三层看门狗检测

| 层级 | 检测目标 | 超时阈值 | 触发动作 |
|------|---------|---------|---------|
| 连接建立 | `run_forever()` 卡在 TCP 连接阶段 | `silence_timeout` | 强制关闭 socket |
| 数据静默 | 完全无数据（TCP 静默断开） | `silence_timeout` | 强制关闭 socket + ws |
| 业务消息 | 有数据但无交互类消息（假活） | `silence_timeout` | 强制关闭 socket + ws |

> **业务消息看门狗只认交互类消息**（chat/gift/like/member/social/fansclub/emoji），
> `RoomRankMessage`、`RoomStatsMessage` 等系统级消息不重置计时器。

### 等待开播

程序默认行为：启动时若直播间未开播，自动等待；采集过程中下播，自动等待重开播。

```
默认 (live_stop: false):  未开播 → 等待 → 开播 → 采集 → 下播 → 等待 → 重开播 → 循环
停止 (live_stop: true):   开播 → 采集 → 下播 → 退出
```

检测机制采用双层方案：
- **主检测** — WebSocket 交互消息（毫秒级，零额外请求）
- **兜底** — HTTP 轮询 `live_check_interval` 秒间隔

### 多房间模式

在 `rooms.txt` 中配置多个房间：

```
126833924894,张君雅
371992233267,Polaris熠熠
```

- 每个房间独立线程，独立 WebSocket 连接
- 控制台状态面板轮显（`[1/3] 张君雅 1234条(5.2m/s)`）
- 日志自动添加 `[主播名]` 前缀，区分来源
- 各房间数据独立写入 `data/{live_id}/` 目录

## 数据输出

### 目录结构

```
data/{live_id}/
├── {YYYYMMDD}_{HHMM}_{roomId}/
│   ├── chat.csv
│   ├── gift.csv
│   ├── like.csv
│   ├── social.csv
│   ├── stats.csv
│   ├── control.csv
│   └── ...
└── ...
```

- 按直播 ID 建文件夹，每次采集生成独立会话目录
- 会话目录命名：`{日期}_{时间}_{room_id}`
- 同一房间多次采集不覆盖，每次生成独立目录
- 延迟创建：无数据不产生空文件
- CSV UTF-8 BOM 编码，Excel 直接打开

### CSV 字段

| 类型 | 字段 |
|------|------|
| chat / lucky_bag | time, user_id, user_name, content, grade, fans_club |
| gift | time, user_id, user_name, gift_name, gift_count, diamond_total, grade, fans_club |
| like | time, user_id, user_name, count, total, grade, fans_club |
| member | time, user_id, user_name, gender, grade, fans_club, member_count |
| social | time, user_id, user_name, action, follow_count, grade, fans_club |
| stats | time, current, total_pv, total_user, online_anchor |
| control | time, status |

### JSONL 示例

```json
{"time": "08:36:55", "user_id": "97992671880", "user_name": "幸运星陈", "content": "让我看看哪个美女好看？", "grade": "[等级6]", "fans_club": "[粉丝团 Lv1]"}
```

## 项目结构

```
├── main.py             启动入口（参数解析、交互选择、多房间管理）
├── config.yaml         运行配置
├── sign.js             签名脚本（Node.js）
├── cookie.txt          登录 Cookie（需自行创建）
├── cookie.example.txt  Cookie 样本文件
├── rooms.txt           房间列表
├── requirements.txt    Python 依赖
├── base/               基础层
│   ├── messages.py     Protobuf 消息定义（PushFrame / Response / 13 种业务消息）
│   ├── parser.py       消息解析与分发表（HANDLERS 字典）
│   ├── output.py       异步日志 + 数据记录器 + 吞吐统计
│   └── utils.py        配置加载、Cookie 解析、常量、工具函数
└── service/            服务层
    ├── fetcher.py      采集器主类（连接管理、消息分发、心跳、看门狗）
    ├── network.py      HTTP 请求 + WebSocket URL 构建 + 房间 API
    └── signer.py       X-Bogus 签名生成（subprocess 调用 Node.js）
```

## 常见问题

### 程序假死（有连接但无弹幕）

**现象**：日志显示 `[数据] 就绪` 后无弹幕，看门狗静默时间不断重置，只有低价值消息。

**原因**：WebSocket 连接建立后，服务端未下发业务消息（仅系统通知）。

**解决**：业务消息看门狗会自动检测并重连。

### 程序假死（完全无数据）

**现象**：日志显示 `[数据] 就绪` 后完全无任何输出，看门狗也不打印。

**原因**：TCP 静默断开，`run_forever()` 阻塞在 `recv()`。

**解决**：`ping_timeout=10` + 看门狗强制关闭 socket。

### DEVICE_BLOCKED

WebSocket 握手返回 `DEVICE_BLOCKED` 通常是 **X-Bogus 签名问题**，而非 IP 或 Cookie 问题。

1. 用 `--log-level DEBUG` 检查签名输出是否正常
2. 确认 Node.js 版本 ≥ 16
3. 检查 `sign.js` 的 polyfill 兼容性

### Cookie 过期

Cookie 过期不影响基本弹幕采集。登录态仅影响礼物详情等少数字段，过期后重新从浏览器导出即可。

### 签名脚本失效

`sign.js` 来自 [DouYin_Spider](https://github.com/cv-cat/DouYin_Spider)，可能随抖音版本更新失效，需更新脚本。

### 首次连接无弹幕，重启后正常

**现象**：程序首次连接后只有低价值消息，重启后立即有弹幕。

**原因**：服务端对新 WebSocket 连接的消息订阅存在延迟或异常。

**解决**：业务消息看门狗 60 秒后自动重连，无需手动重启。

## 更新记录

### 2026-05-05

- 房间配置从 `config.yaml` 移至独立的 `rooms.txt` 文件
- 新格式更简洁：每行 `id,name`，`#` 开头表示禁用
- 自动更新主播名功能保持不变 只输入房间 ID 即可在采集时自动更新主播名

## 依赖

| 包名 | 用途 |
|------|------|
| `requests` | HTTP 请求 |
| `websocket-client` | WebSocket 连接 |
| `proto-plus` | Protobuf 序列化/反序列化 |
| `protobuf` | Protobuf 运行时 |
| `pyyaml` | YAML 配置解析 |

## 免责声明

本项目仅供学习研究使用，请勿用于商业用途或违反平台规则的行为。采集的数据仅用于技术研究，请勿传播或用于非法目的。

## 致谢

- [DouYin_Spider](https://github.com/cv-cat/DouYin_Spider) — 签名脚本参考
- [DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher) — 弹幕爬取参考
