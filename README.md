# DouyinBarrage

> 抖音直播间弹幕实时采集器 — WebSocket 长连接，13 种消息类型，CSV / SQLite 双格式，集成直播录制。

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![Node.js](https://img.shields.io/badge/Node.js-20+-green.svg)
![License](https://img.shields.io/badge/License-MIT-lightgrey.svg)

## 快速开始

```bash
# 安装
pip install -r requirements.txt

# 配置 Cookie（可选，获取完整礼物等数据）
cp cookie.example.txt cookie.txt
# 浏览器登录 douyin.com → F12 → Cookies → 复制到 cookie.txt

# 编辑 rooms.txt（每行: id,name，#开头不启用）
echo "126833924894,张君雅" >> rooms.txt

# 运行
python main.py --all          # 采集全部房间
python main.py 536863152858   # 指定房间
python main.py --all --record # 采集 + 录制
```

## 命令行参数

| 参数 | 说明 |
|------|------|
| `live_id` | 直播间 ID（不提供则交互式选择） |
| `--log-level {DEBUG,INFO,WARNING,ERROR,NONE}` | 覆盖日志级别 |
| `--live-stop` | 直播结束后停止退出 |
| `--live-wait` | 直播结束后等待重开播 |
| `--record` | 启用直播录制 |
| `--all` | 采集 rooms.txt 全部房间 |

## 配置文件

编辑 `config.yaml`：

```yaml
output_dir: data              # 输出目录
live_stop: true               # 下播后退出（false=等待重开）
live_check_interval: 160      # 未开播轮询间隔（秒）

output:                       # 消息类型开关
  chat: true                  # 弹幕
  gift: true                  # 礼物
  like: true                  # 点赞
  social: true                # 关注/分享
  stats: true                 # 统计
  lucky_bag: true             # 福袋口令
  control: true               # 直播状态
  # member / rank / fansclub / emoji / room / roomstats: false

barrage:
  csv: true                   # CSV 输出
  sqlite: false               # SQLite 输出

record:
  enabled: false              # 录制开关
  format: ts                  # 封装: ts / flv / mp4
  quality: 原画               # 画质: 原画/超清/高清/标清/省流
  segment_time: 0             # 分段时长（秒），0=不分段
  segment_size: 0             # 分段大小（MB），0=不限制
  auto_convert: true          # 结束后自动 ts→mp4
  recheck_delay: 10           # 下播后等待重开秒数

api:
  enabled: false              # HTTP API 开关
  port: 8088                  # 监听端口
```

## 数据输出

```
data/{主播名}/
├── data.db                   # SQLite 数据库（跨会话追加）
├── meta.json                 # 主播信息
└── YYYYMMDD_HHMM/           # 会话目录
    ├── chat.csv / gift.csv / ...
    └── {主播名}_*.ts        # 录制文件（同目录）
```

- 日志按天：`YYYY-MM-DD.log`，启动时自动清理 7 天前
- 无效房间自动注释到 `rooms.txt`，不再重试

## 常见问题

| 现象 | 解决 |
|------|------|
| DEVICE_BLOCKED | X-Bogus 签名问题，检查 Node.js ≥ 16，`--log-level DEBUG` |
| 有连接无弹幕 | 业务看门狗 60s 后自动重连，无需手动干预 |
| 完全无数据 | TCP 静默断开，ping_timeout + 看门狗自动处理 |
| 无效房间卡住 | 自动检测并注释，不重试 |

## 更新记录

详见 [CHANGELOG.md](CHANGELOG.md)

## 免责声明

本项目仅供学习研究，请勿用于商业用途或违反平台规则。

## 致谢

- [DouYin_Spider](https://github.com/cv-cat/DouYin_Spider) — 签名脚本
- [DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher) — 弹幕爬取
- [DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) — 录制架构
