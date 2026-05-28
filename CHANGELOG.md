# 更新记录

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
