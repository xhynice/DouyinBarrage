# 更新记录

## 2026-05-29

### ⚠️ 历史数据修复

**跨夜时间字段修正**：2026-05-29 之前的采集数据存在跨夜时间字段错误——跨午夜的会话（如 21:03 开播至次日 00:30 下播），CSV `time` 字段中次日凌晨的时间仍标记为当天日期（如 `2026-05-07 00:31:59` 实际应为 `2026-05-08 00:31:59`）。

受影响的范围：
- 所有跨午夜会话的 CSV 文件（chat、gift、like、social、stats、control 等）
- 以 `control.csv` 最为明显（仅一行"已结束"记录，无前后行对比检测）

已通过一次性修复脚本批量修正所有历史数据。**如果你在 2026-05-29 之前采集过数据，且本地数据未更新，请重新拉取最新数据或手动运行修复。**

### 前端（弹幕查看器）

- 新增弹幕数据前端查看页面（`docs/index.html`），支持多直播间切换、会话选择、搜索筛选
- 会话目录格式统一为 `YYYYMMDD_HHMM`（如 `20260529_1148`），旧格式自动迁移
- 级联下拉选择：年月 → 会话列表，支持滚动
- 月份筛选仅加载目标月份数据，不加载全部会话
- 搜索支持内容搜索和 @用户搜索
- 消息类型筛选、排行榜统计展示
- 切换会话/主播时自动重置所有筛选状态
- `build_barrage.py` 改为仅使用 CSV 构建（移除 SQLite 构建路径）

### 变更

- 统一单/多房间模式，删除 `start_single_room()`，所有入口统一走 `main_multi()`，单房间也有热加载
- 移除 `BARRAGE` 自定义日志级别，弹幕消息统一使用 `DEBUG` 级别
- 移除 JSONL 输出格式，仅保留 CSV + SQLite
- 移除 `proxy` 代理配置
- 移除 `multi_room` 参数和单房间光标动画，统一使用多行状态面板
- 日志文件改为按大小轮转（`RotatingFileHandler`，5MB × 3 份）
- CSV `time` 字段格式从 `HH:MM:SS` 改为 `YYYY-MM-DD HH:MM:SS`
- 简化日志输出：移除 ANSI 转义码、`\r` 单行刷新等终端控制逻辑
- 数据目录命名从 `{live_id}` 改为 `{主播名}`，会话目录格式改为 `YYYYMMDD_HHMM`
- 网络、重连、统计配置改为硬编码常量（从 config.yaml 移除），`max_reconnects` 3→5，`reconnect_base_delay` 2→8，`rcvbuf_kb` 256→512
- `file_format` 改为 `csv` / `sqlite` 两个独立布尔开关，`gift_combo_final`/`csv`/`sqlite`/`file_dir` 从 `output` 拆出为独立的 `format` 配置段
- `cookie_file` 改为硬编码 `cookie.txt`（从 config.yaml 移除）
- 修复 `_save_room_info()` 用 `live_id` 做目录名的 bug，改为 `anchor_name`（与 DataRecorder 一致）
- 移除死代码：`safe_time()`、`_state_json()`、`_log_status()`
- 网络常量不再赋值给实例变量，直接使用类常量
- `DataRecorder.open()` 移除未使用的 `room_id` 参数

## 2026-05-28

### 新增

- 新增 `gift_combo_final` 配置项，开启后礼物连击只记录最终值（x520），丢弃中间递增消息（x1, x2, ..., x519）
- 新增 SQLite 输出格式，支持 `file_format` 配置任意格式组合（空格分隔，如 `csv sqlite`、`csv json sqlite`）
- SQLite 数据库位于房间级目录 `data/{live_id}/barrage.db`，跨多次采集自动追加
- SQLite `time` 字段存储 Unix 秒级时间戳（INTEGER），跨午夜无歧义，视频+弹幕同步只需简单减法
- 数值字段（`gift_count`、`diamond_total`、`count`、`total` 等）自动映射为 SQLite INTEGER 类型
- SQLite 采用 WAL 模式 + `synchronous=NORMAL` + 8MB 页缓存，兼顾并发安全与写入性能

### 变更

- `file_format` 配置方式变更：移除 `both`/`none` 关键字，改为任意格式名空格分隔组合（`csv json sqlite`），留空即不保存
- 吞吐统计打印间隔默认值从 300s 调整为 60s

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
