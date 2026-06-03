# 更新记录

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
