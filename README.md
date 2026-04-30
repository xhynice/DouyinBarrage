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

## 快速开始

### 环境要求

- Python 3.11+
- Node.js v20+（执行签名脚本）

### 安装

```bash
cd douyin-barrage
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
| `--log-level` | 覆盖日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` / `NONE` |
| `--live-stop` | 直播结束后停止退出（默认跟随配置文件） |
| `--live-wait` | 直播结束后等待重开播（默认跟随配置文件） |

## Cookie 配置

未登录时部分消息（如礼物详情）可能受限。提供登录 Cookie 可获取完整数据。

### 获取方式

1. 浏览器登录 [抖音](https://www.douyin.com)
2. 打开开发者工具 → Application → Cookies → `douyin.com`
3. 全选复制所有 Cookie，粘贴到项目根目录 `cookie.txt`

### 支持格式

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

> ⚠️ Cookie 包含敏感信息，请勿分享或提交到版本控制。

## 配置说明

编辑 `config.yaml`：

```yaml
# ==================== 多房间配置 ====================
rooms:
  - id: "126833924894"           # 直播间 ID
    name: "张君雅"               # 主播名（可选，首次连接自动补全）
    enabled: true                # 是否采集（false=跳过）

log_level: INFO              # 日志级别: DEBUG / INFO / WARNING / ERROR / NONE
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

## 消息类型

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

## 数据输出

### 目录结构

```
data/{live_id}/{年月}/
├── {YYYYMMDD}_{HHMM}_{roomId}_chat.csv
├── {YYYYMMDD}_{HHMM}_{roomId}_gift.csv
├── {YYYYMMDD}_{HHMM}_{roomId}_like.csv
├── {YYYYMMDD}_{HHMM}_{roomId}_social.csv
├── {YYYYMMDD}_{HHMM}_{roomId}_stats.csv
├── {YYYYMMDD}_{HHMM}_{roomId}_control.csv
└── ...
```

- 按直播 ID 建文件夹，按年月分目录
- 时间优先命名，文件列表按时间排序
- 同一房间多次采集不覆盖，每次生成独立文件
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

## 运行机制

```
1. 加载 Cookie    → 从 cookie.txt 加载登录态，否则游客模式
2. 获取 ttwid     → HTTP 请求 live.douyin.com 获取（懒加载，首次访问触发）
3. 获取 roomId    → 调用 enter_room_api 解析直播间信息
4. 生成签名       → 13 参数拼接 → MD5 → Node.js 执行 sign.js → X-Bogus
5. WebSocket 连接 → 携带登录 Cookie + 签名建立长连接
6. 消息处理       → PushFrame → gzip 解压 → Response → 按类型分发
7. 数据输出       → 异步日志 + CSV/JSONL 批量写入（2s 刷新）
```

## 线程模型

```
主线程 (_connectWebSocket)
│   WebSocket 连接循环（含重连逻辑）
│   run_forever() 阻塞在此
│
├── heartbeat (daemon)    每 10s 发送心跳包
├── watchdog  (daemon)    每 10s 检查：
│   ├── 连接建立超时（60s 未建立连接）
│   ├── 数据静默超时（60s 无任何数据）
│   └── 业务消息超时（60s 有数据但无业务消息）← 新增
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
> `_last_business_msg_time` 为 0 时（从未收到业务消息），使用连接建立时间作为基准。

## 等待开播

程序默认行为：启动时若直播间未开播，自动等待；采集过程中下播，自动等待重开播。

```
默认 (live_stop: false):  未开播 → 等待 → 开播 → 采集 → 下播 → 等待 → 重开播 → 循环
停止 (live_stop: true):   开播 → 采集 → 下播 → 退出
```

检测机制采用双层方案：
- **主检测** — WebSocket 交互消息（毫秒级，零额外请求）
- **兜底** — HTTP 轮询 `live_check_interval` 秒间隔

## 多房间模式

```yaml
rooms:
  - id: "126833924894"
    name: "张君雅"
    enabled: true
  - id: "371992233267"
    name: "Polaris熠熠"
    enabled: true
```

- 每个房间独立线程，独立 WebSocket 连接
- 控制台状态面板轮显（`[1/3] 张君雅 1234条(5.2m/s)`）
- 日志自动添加 `[主播名]` 前缀，区分来源
- 各房间数据独立写入 `data/{live_id}/` 目录

## 项目结构

```
├── main.py             启动入口（参数解析、交互选择、多房间管理）
├── config.yaml         运行配置
├── sign.js             签名脚本（Node.js）
├── cookie.txt          登录 Cookie
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

## 修复记录

### v1.1 — 假死修复（2026-04-26）

解决了程序在特定网络条件下"假死"（连接在但不采集）的问题。

#### 修复 1：`ping_timeout` 检测无响应连接

**问题**：`websocket-client` 的 `run_forever()` 只发 ping 不检测 pong，TCP 静默断开时永久阻塞。

**修复**：添加 `ping_timeout=10`，10 秒内没收到 pong 自动断开。

```python
self.ws.run_forever(
    ping_interval=30,
    ping_timeout=10,    # ← 新增
    ...
)
```

#### 修复 2：看门狗强制关闭底层 socket

**问题**：看门狗调用 `self.ws.close()` 时，如果 socket 已死，`close()` 本身可能永久阻塞。

**修复**：先 `self.ws.sock.close()` 暴力关闭底层 TCP，再 `self.ws.close()` 做清理。

```python
self.ws.keep_running = False
if self.ws.sock:
    self.ws.sock.close()    # 强制关闭底层 socket
self.ws.close()
```

#### 修复 3：`_enter_wait_mode()` 同样强制关闭

**问题**：下播进入等待模式时，`close()` 同样可能阻塞。

**修复**：同修复 2，先关底层 socket。

#### 修复 4：看门狗检测连接建立超时

**问题**：`run_forever()` 卡在 TCP 握手阶段时，`_connected_event` 一直为 False，看门狗直接跳过。

**修复**：看门狗记录启动时间，连接建立阶段也检测超时。

#### 修复 5：重连计数器连接成功后重置

**问题**：网络抖动频繁重连后，即使恢复稳定，指数退避延迟仍很长。

**修复**：`_wsOnOpen` 中重置 `_reconnect_count = 0`。

#### 修复 6：`stop()` 中强制关闭 socket

**问题**：用户按 Ctrl+C 时，`stop()` 调用 `ws.close()` 可能阻塞，程序无法及时退出。

**修复**：同修复 2，先关底层 socket。

#### 修复 7：`fetch_ttwid()` session 泄漏

**问题**：`http_get_with_retry` 抛异常时，`ssr_session` 未关闭。

**修复**：使用 `try/finally` 确保关闭。

#### 修复 8：`generate_signature()` proc 未定义

**问题**：`subprocess.run()` 抛异常时，`proc` 变量未定义，后续 `except` 引用会抛 `NameError`。

**修复**：`proc = None` 初始化，异常处理中安全访问。

#### 修复 9：`DataRecorder._do_flush()` 回退无锁

**问题**：CSV/JSONL 写入失败后，数据回退操作无锁保护，可能与 `record()` 并发。

**修复**：回退操作加 `_record_lock`。

### v1.2 — 假活修复（2026-04-26）

解决了程序"假活"（WebSocket 有数据但无业务消息）的问题。

#### 修复 10：业务消息看门狗

**问题**：低价值消息（系统通知、Banner 等）不断刷新 `_last_msg_time`，看门狗认为连接正常，但实际没有弹幕/礼物等业务消息。

**修复**：新增 `_last_business_msg_time`，独立追踪业务消息时间。看门狗同时检查：
- `_last_msg_time`：任意数据静默超时
- `_last_business_msg_time`：业务消息静默超时

如果 60 秒内有数据但无业务消息，触发自动重连。

日志输出：
```
[看门狗] 60s 无业务消息 (仅有低价值消息)，触发重连
```

### v1.3 — 假活修复·续（2026-04-27）

v1.2 的业务消息看门狗在特定场景下仍然失效。

#### 修复 11：排除系统级消息对业务计时器的干扰

**问题**：`RoomRankMessage`、`RoomStatsMessage` 等系统级消息在未开播时也会推送，
且在 HANDLERS 中有注册，导致 `_last_business_msg_time` 被不断重置，
看门狗永远无法达到沉默阈值，无法检测"连接成功但只有系统消息"的假活状态。

实测数据：
- 开播房间 26 秒内收到 258 条业务消息（礼物/聊天/点赞等）
- 未开播房间 26 秒内收到 `RoomRankMessage:4` + `RoomStatsMessage:4`（系统消息）

**修复**：`_last_business_msg_time` 仅在 `INTERACTIVE_TYPES`（chat/gift/like/member/social/fansclub/emoji）
到达时更新，排除系统级消息。

#### 修复 12：连接建立时间基准

**问题**：v1.2 中 `_last_business_msg_time` 在 `_wsOnOpen` 中初始化为当前时间，
导致看门狗在连接后 60 秒内不会检测业务沉默——即使从未收到任何业务消息。

**修复**：`_last_business_msg_time` 保持为 0，看门狗在 `_last_business_msg_time == 0` 时
使用 `_ws_connected_at`（连接建立时间）作为基准计算沉默时长，
确保"连接后从未收到业务消息"的情况能在 60 秒内被检测到。

#### 修复 13：每次连接刷新 user_unique_id

**问题**：`user_unique_id` 在程序启动时生成一次，之后所有连接（包括等待期间的 HTTP 轮询
和后续的 WebSocket）都复用同一个值。服务端可能将长期用于 HTTP 轮询的 ID 标记为低信任，
导致后续 WebSocket 连接被降级——只推系统消息，不推业务消息。

**修复**：每次建立 WebSocket 连接前重新生成 `user_unique_id`，确保每次连接使用全新 ID。

#### 修复 14：等待开播后延迟连接 + 刷新 ttwid

**问题**：HTTP 轮询检测到开播后立即建立 WebSocket 连接，此时：
1. 新 room_id 的消息路由可能尚未初始化完成
2. ttwid 是等待期间获取的，可能关联了旧的会话状态

**修复**：检测到开播后等待 5 秒再建立 WebSocket，同时刷新 ttwid。

## 常见问题

### 程序假死（有连接但无弹幕）

**现象**：日志显示 `[数据] 就绪` 后无弹幕，看门狗静默时间不断重置，只有低价值消息。

**原因**：WebSocket 连接建立后，服务端未下发业务消息（仅系统通知）。

**解决**：v1.2 + v1.3 已修复，业务消息看门狗会自动检测并重连。

### 程序假死（完全无数据）

**现象**：日志显示 `[数据] 就绪` 后完全无任何输出，看门狗也不打印。

**原因**：TCP 静默断开，`run_forever()` 阻塞在 `recv()`。

**解决**：v1.1 已修复，`ping_timeout=10` + 看门狗强制关闭 socket。

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

**原因**：服务端对新 WebSocket 连接的消息订阅存在延迟或异常，尤其在等待开播后首次连接时容易出现。

**解决**：v1.2 + v1.3 已修复，业务消息看门狗 60 秒后自动重连，无需手动重启。

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
