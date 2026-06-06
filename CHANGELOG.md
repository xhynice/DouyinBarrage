# 更新记录

## 2026-06-06

### 清理

- **删除死代码：备用 API `scrape_room_info` 及其辅助函数 `_find_room_in_dict`**
  - 文件：`service/network.py` (74+17 行), `test_stream_url.py` (import + 调用)
  - 原因：抖音改用 `LIVE_SSR_DATA_ID` SSR 标签（不再是 `RENDER_DATA`），HTML 抓取拿不到房间信息，备用 API 实际已失效
  - 影响：主程序现在只有 `enter_room_api` 一条路，无降级
  - 后续：如需备用 API，需逆向抖音新页面结构或找新数据源

### 变更

- **控制消息 `[直播状态] 开始/暂停/已结束` 提升到 INFO 级别** (`service/fetcher.py:1177-1183`)
  - 原：所有消息统一 `logger.debug`，INFO 模式下完全看不到控制消息
  - 新：只对 `type=='control'` 用 `logger.info`，普通消息（弹幕/礼物/点赞等）保持 DEBUG 避免刷屏
  - 效果：下播时日志会清晰显示 `[直播状态] 已结束`

### 修复

- **看门狗"连接建立超时"误判** (`service/fetcher.py:905`)
  - 原 bug：`elapsed = time.time() - watchdog_start`，但 `watchdog_start = time.monotonic()` (line 893)
  - 两者时钟不同源（wall clock vs monotonic），差值约 1.78e9 秒（≈56 年）
  - 触发场景：下播/重连后 WS 未连上时，看门狗首次 check 就误报"超时"，强制重连
  - 严重后果：5 次重连耗尽后程序停止（其实是"误以为卡死"），无法自动恢复
  - 修复：`elapsed = time.monotonic() - watchdog_start`，时钟一致

### 变更（破坏性）

- **画质名称与抖音官方 API 对齐**
  - 旧：原画 / 超清 / 高清 / 标清 / 省流
  - 新：原画 / **蓝光** / **超清** / **高清** / **标清**（对应官方 level 5/4/3/2/1）
  - 数据来源：`live_core_sdk_data.pull_data.options.qualities`
  - 影响：`config.yaml` 中 `quality: 标清` 现在指官方"标清" (720x540@25 1Mbps) 而非之前的高清 (960x720@30 2Mbps)；`省流` 选项移除（未识别回退到"原画"）
  - `_KEY_TO_INDEX` index 不变，只改 label

### 修复

- 修复开播瞬间 API 状态抖动导致录制误判下播退出的问题
  - 重构 `DouyinRecorder`：去掉 `stream_url_provider` / `live_status_provider` 回调和"下播二次确认"逻辑
  - recorder 不再做 API 状态判断，ffmpeg 异常退出仅通过 `on_failure` 回调通知 fetcher
  - 录制在 `WebSocket 握手成功` 后立即启动（不再等首条业务消息门），recorder 不再二次查询 API 状态

### 新增

- 录制自愈：ffmpeg 异常退出后由看门狗 30s 背压后自动重启

### 变更

- 守护线程从 4 个合并为 3 个：`_heartbeat_loop` + `_watchdog_loop` (v3 合并 WS 健康+录制自愈) + `_stats_loop`
- 录制自愈 30s 背压：避免 ffmpeg 反复崩时看门狗热循环重试
- 删除首条业务消息门（`prev==0` 触发录制分支）：WS 握手成功即代表房间存在，无需等待
- 删除 `_recording_pending` 状态字段、`_recording_watchdog_loop`、`_recording_watchdog_thread`、`_stop_recorder_for_reconnect`
- WS 重连不再主动停掉旧 ffmpeg：推流地址与 WS 通道独立，ffmpeg 自带 `-reconnect_streamed` 自愈，主动重启会制造不必要空窗和文件碎片
- 删除 5s warmup 和 90s 业务消息静默门（由 WS 健康检查 + 30s 背压兜底）
- `_start_recording` 的"画质=xxx 地址=..."日志仅在 ffmpeg 真正启动后才输出（之前是无论 ffmpeg 是否启动都会打印，误导排障）
- `DouyinRecorder.__init__` 签名变更：`stream_url_provider` / `live_status_provider` → `on_failure`
- 移除死代码 `_refresh_stream_url`、`check_live_status`、`_recheck_live_status`

## 2026-06-03

### 新增

- 直播流录制（`DouyinRecorder`），FFmpeg 子进程，支持 ts/flv/mp4 + 分段 + 自动转码
- 画质选择（原画/超清/高清/标清/省流），支持自动降级和连通性检测
- `--record` / `--all` 命令行参数
- WebSocket 预请求：连接前获取服务端 `cursor`，提高连接稳定性
- 多 API 降级：主 API 失败后自动降级到 HTML 页面解析
- 下播二次确认：流中断后等待 `recheck_delay` 秒再确认，避免误判
- HTTP API 服务（`service/api.py`）：`/api/status`、`/api/rooms`、`/api/rooms/:id`，默认关闭
- 状态栏显示弹幕格式、当前时间

### 变更

- Cookie 登录用户名隐藏显示（`一***`）
- 移除 `config.yaml` 中 `log_level`，默认 INFO，调试用 `--log-level DEBUG`
- `barrage.file_dir` + `record.file_dir` 合并为顶层 `output_dir`
- 控制台改为单行状态行模式（5s 间隔），取消清屏和分隔符
- 看门狗首检 3s→30s，常规 30s→60s
- 配置键改名：`format:` → `barrage:`
- `recheck_delay` 从配置移除，改为内置常量

### 优化

- 提取 `_close_ws()`、`_query_room_api()`、`_query_room_with_fallback()`、`_refresh_ttwid()` 方法，消除重复代码
- `sanitize_dir_name()`、`DEFAULT_CONFIG` 移至 `base/utils.py` 共享
- 线程安全：`_close_ws()` 增加 `_ws_lock`，DataRecorder 合并锁消除竞态
- ffmpeg stderr 捕获，退出码 255 降级为 debug
- mp4 添加 `-movflags frag_keyframe+empty_moov`，断线后可播放
- 重连前 API 下播检测，避免无效重连
- 录制文件名统一 `%Y%m%d_%H%M` 格式，与弹幕目录一致，带毫秒防冲突
- `meta.json` 不再保存推流地址（`stream_url`）

## 2026-05-28

### 新增

- SQLite 输出格式，WAL 模式 + INTEGER 类型字段，跨会话追加
- `gift_combo_final` 配置项，礼物连击只记录最终值
- 弹幕查看器前端：级联会话选择、内容搜索、排行榜统计、礼物抖币计算
- `build_barrage.py` 数据构建脚本（CSV → JSON）

### 变更

- 统一单/多房间模式，所有入口走 `main_multi()`，单房间也有热加载
- 数据目录从 `{live_id}` 改为 `{主播名}`，会话目录格式 `YYYYMMDD_HHMM`
- 日志按大小轮转（5MB × 3 份），移除 BARRAGE 自定义级别，弹幕统一 DEBUG
- 网络/重连配置改为硬编码常量，`max_reconnects` 3→5，`reconnect_base_delay` 2→8
- `cookie_file` 硬编码 `cookie.txt`，`file_format` 改为 `csv`/`sqlite` 独立开关
- 房间配置从 `config.yaml` 移至 `rooms.txt`

## 2026-05-09

### 修复

- 修复礼物统计逻辑：按 `gift_count` 递增识别连送，避免重复统计
- 删除 `docs/data/` 构建产物，不再上传到 GitHub（改为 Actions 实时构建）

### 优化

- 更新 GitHub Description 和 Topics

## 2026-05-05

### 变更

- 房间配置从 `config.yaml` 移至独立的 `rooms.txt` 文件
- 新格式更简洁：每行 `id,name`，`#` 开头表示禁用
- 自动更新主播名功能保持不变，只输入房间 ID 即可在采集时自动更新主播名
