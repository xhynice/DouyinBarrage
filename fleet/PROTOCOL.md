# Douyin Livestream Monitoring — Workstation Operator Protocol

Audience: a research assistant (RA) running **one standalone workstation** that records ~40
Douyin livestream rooms during a fixed nightly window (e.g. 20:00–22:00) for ~30 days, uploads
each night's data to a ModelScope dataset, and clears local disk.

You do **not** need to understand the internals. Follow the steps; watch the morning brief.

---

## 0. What it does (the nightly cycle)

```
20:00  start recording ~40 rooms (danmaku CSV + SD video)
22:00  stop → convert to .mp4 → align chat-to-video → pack into <=7GB tar shards
       → upload to ModelScope + verify → delete local video → keep a small text copy
       → write a status line you read the next morning
```
Everything after 20:00 is automatic (cron). Your job is **setup once**, then **check the brief each morning**.

---

## 1. Prerequisites

**The workstation must have:**
- **Linux** (Ubuntu 22.04+ or similar).
- **≥150 GB free disk** on the drive holding the project. An SD (标清) night is ≈30–40 GB; nightly
  upload+purge keeps it flat, but peak usage is ~2× a night (raw video + upload cache) *before*
  purge, and a night whose upload fails is retained — so keep real headroom.
- **Download bandwidth — during recording (hard, real-time).** Must sustain the *sum* of all live
  streams simultaneously: SD (标清) ≈ 1 Mbps/room → **~40 Mbps down for 40 rooms** (原画 ≈ 5–6×).
  Live streams cannot be buffered or caught up — if the pull falls behind, frames are **permanently
  dropped**. Use a **direct** connection (no throttling proxy/VPN); `record.sh` defaults to direct
  for this reason. Give headroom above the raw sum.
- **Upload bandwidth — after the window.** ≥50 Mbps up recommended. The night (~30–40 GB) uploads
  overnight; ModelScope throttles, so it can take 1–3 h — fine as long as the machine stays on. A
  slow upload only delays; nothing is lost.
- **Always-on** — auto **sleep / suspend / hibernate / shutdown disabled**. If the machine sleeps
  mid-recording or mid-upload, that night is lost or left unverified. Easiest thing to get wrong.
- **SSH access** — to operate and monitor the machine remotely.

**Software** (installed once, §2.1): Python 3, ffmpeg, Node 20+, git, git-lfs. Installing via `apt`
needs admin/**sudo once**; after setup the nightly run needs **no elevated privileges**. (No sudo?
They can go in user space — `nvm` for Node, a static ffmpeg binary, the Python venv.)

**Accounts / secrets (get from the PI before starting):**
- A **ModelScope access token** with **write** access to the dataset `SISU_DynCogLab/douyin`.
- A **Douyin cookie** (recommended — complete gift/data; without it you collect as guest with a few
  limited fields).
- The **room list** (`rooms.txt`) if not already committed.

---

## 2. One-time setup

### 2.1 Install system software
```bash
# Debian/Ubuntu
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg git git-lfs
git lfs install
# Node.js 20+ (required for Douyin request signing). If `node --version` is < 20:
#   use nvm or NodeSource to install Node 20+.
node --version    # must be >= 20
ffmpeg -version   # must exist
```

### 2.2 Get the project
```bash
cd ~                      # or wherever you keep projects
git clone git@github.com:wangruosi/DouyinBarrage.git
cd DouyinBarrage
git checkout feat/timeline-sidecar   # REQUIRED: the fleet code lives on this branch, not the default
```
Verify you're on the right branch and the fleet layer is present:
```bash
git branch --show-current            # -> feat/timeline-sidecar
ls fleet/                            # -> nightly.sh postrun.sh pack.py upload.sh station.env PROTOCOL.md
```

### 2.3 Python environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2.4 Douyin cookie (recommended)
```bash
cp cookie.example.txt cookie.txt
# In a desktop browser: log in to douyin.com → F12 → Application/Storage → Cookies →
# copy the full cookie string into cookie.txt (one line). Save.
# (cookie.txt is gitignored — it never leaves this machine.)
```
Skipping this still works (guest mode) but gift/some fields are limited.

### 2.5 Room list
`rooms.txt` — one room per line, `id,name` (lines starting with `#` are disabled):
```
56697889278,与辉同行
dongfangzhenxuan,东方甄选
...
```
Confirm it has your intended ~40 rooms: `grep -vc '^#' rooms.txt`.
**Finalize this BEFORE the nightly window — do not edit rooms.txt while recording is running.**

### 2.6 Clone the ModelScope dataset (the upload target)
This is a **separate** git repo used only as an upload vehicle. Clone it *next to* the project,
using your token (skip downloading existing large files):
```bash
cd ~   # same parent as DouyinBarrage
GIT_LFS_SKIP_SMUDGE=1 git clone \
  "https://oauth2:<YOUR_MODELSCOPE_TOKEN>@www.modelscope.cn/datasets/SISU_DynCogLab/douyin.git" \
  douyin
cd douyin
git config user.name  "station st01"
git config user.email "you@example.org"
git config lfs.fetchexclude "*"      # never pull other stations' large files
cd ~/DouyinBarrage
```
> Security: the token sits in this clone's git config only. Do not paste it anywhere else or commit it.

### 2.7 Configure this station
Edit `fleet/station.env` — set just three things:
```bash
STATION=st01                         # unique id for THIS workstation
REPO=/home/<you>/douyin              # absolute path to the dataset clone from 2.6
START_AT=20:00                       # window start (local time)
MINUTES=120                          # window length (2h)
```
(`APP_DIR`/`DATA_DIR` auto-derive — leave them.)

### 2.8 Verify recording config
`config.yaml` should already have (confirm):
```yaml
record:
  quality: 标清         # SD; keep unless the PI wants 原画 (needs ~5x disk/bandwidth)
  auto_convert: false   # conversion is done by postrun, off the stop path — leave false
live_stop: false        # keep recording across brief drops within the window
```

---

## 3. Acceptance test (do this once, before scheduling)

Prove the whole pipeline works end-to-end on a short sample. Run during a time when some rooms
are live:
```bash
cd ~/DouyinBarrage
source .venv/bin/activate
scripts/record.sh --minutes 3          # records ~3 min of whatever is live, then graceful-stops
fleet/postrun.sh                        # convert → align → pack → upload → verify → purge → status
```
**Expected final lines:**
```
[upload] VERIFIED
[postrun ...] purge done (data/ ...MB->...MB, .git/lfs ...MB->0MB)
[postrun ...] DONE station=st01 date=<today> idle=true upload=verified disk_free=...GB
```
Then confirm on the website: open
`https://modelscope.cn/datasets/SISU_DynCogLab/douyin/files` → you should see
`video/<today>/st01/…tar`, `text/<today>/st01.tar.gz`, `manifest/<today>/st01.json`.

If you see `VERIFIED` + files on the site + `data/` emptied → **the station is ready.**
If not, see §6 Troubleshooting and tell the PI before proceeding.

---

## 4. Run each night (manual, current mode)

Automatic scheduling (cron) is **not** used yet. For now you launch `nightly.sh` yourself each
evening. It reads `START_AT`/`MINUTES` from `station.env`, waits until the window, records, then
runs the full pipeline (convert → align → pack → upload → verify → purge → status).

Launch it **detached** (keeps running if you close the terminal), any time before `START_AT`:
```bash
cd ~/DouyinBarrage
source .venv/bin/activate                              # so python/node/ffmpeg are on PATH
nohup fleet/nightly.sh > runs/nightly_$(date +%Y%m%d).out 2>&1 &
echo "launched pid $!"
```
It will wait until `START_AT`, record for `MINUTES`, then upload+purge on its own. Watch progress:
```bash
tail -f logs/nightly-$(date +%Y%m%d).log              # Ctrl-C to stop watching (does NOT stop the run)
```
Leave the machine **powered on and awake** (disable sleep/suspend) until the upload finishes.

To change the window: edit `START_AT`/`MINUTES` in `station.env` before launching.

> When you later move to unattended cron scheduling, that's a separate step to add back — it is not
> part of this manual workflow.

---

## 5. Daily monitoring (your morning routine, ~2 min)

Each morning, from `~/DouyinBarrage`:
```bash
# a) the machine is idle (nothing stuck recording):
pgrep -x ffmpeg | wc -l          # expect 0 after the window
# b) last night's brief:
cat archive/manifest/$(date -d yesterday +%Y%m%d)/st01.json | python3 -m json.tool | \
  grep -E '"idle"|"upload"|"summary"|"disk_free' 
# c) disk:
df -h .
# d) last night's run log (tail):
tail -n 30 logs/nightly-$(date -d yesterday +%Y%m%d).log
```

**What "good" looks like:**
- `idle: true`, `upload: "verified"`.
- `summary`: most rooms `recorded`; a few `not_live`/`partial` is normal (not every broadcaster
  streams every night).
- disk free comfortably above 40 GB.

**Red flags → act (see §6/§7):**
| Sign | Meaning | Action |
|---|---|---|
| `upload: "failed"` | tonight's upload didn't verify; data retained locally | re-run upload (§7.3); check internet/token |
| `idle: false` | a recorder was still running after the window | stop it (§7.1); check the log |
| disk free < 40 GB | uploads may be backing up | check for failed nights; free space (§7.3) |
| **no manifest for last night** | the station didn't run at all | check machine was on/awake; check cron (`crontab -l`); check log |
| many rooms `error` (0 chat) | likely cookie expired / device blocked | refresh cookie (§8) |

**Report to the PI daily:** a one-line status (date, idle, upload, #recorded, disk free), plus any red flag.

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `node: command not found` / `DEVICE_BLOCKED` in log | Node missing/too old, or signing failed | install Node ≥20; re-run |
| `ffmpeg: command not found` | ffmpeg not installed / not on PATH | `sudo apt install ffmpeg` |
| Acceptance upload hangs then fails | token wrong/expired, or no write access | verify token with PI; re-clone dataset (2.6) |
| `record.sh: another instance is already running` | a previous run didn't exit | check `pgrep -x ffmpeg`; stop it (§7.1) |
| All rooms `not_live` at test time | nothing is broadcasting right now | test again during evening hours |
| `pack ... no sessions` | no rooms recorded (all offline, or window missed) | confirm rooms.txt + that streams were live |
| Upload very slow (hours) | ModelScope server-side throttling (normal) | let it finish overnight; not an error |

Full run logs: `logs/nightly-YYYYMMDD.log` and `runs/*.log`.

---

## 7. Manual / emergency procedures

### 7.1 Stop everything right now (graceful)
```bash
PY=$(pgrep -f 'main\.py --all' | tail -1)     # the python recorder
[ -n "$PY" ] && kill -INT "$PY"               # graceful: flush + close (a few seconds)
sleep 8; pgrep -x ffmpeg | wc -l              # should reach 0
```
Only if it won't stop: `for p in $(pgrep -x ffmpeg); do kill -9 $p; done` then kill the python.

### 7.2 Run a night manually (if cron was missed)
```bash
cd ~/DouyinBarrage && fleet/nightly.sh         # records the configured window now-ish, then postrun
```
(Or just the pipeline on already-recorded data: `fleet/postrun.sh`.)

### 7.3 Re-try a failed upload / purge
If `upload: failed`, the local data is still there. Re-run just the post-processing:
```bash
cd ~/DouyinBarrage && fleet/postrun.sh
```
It re-packs, re-uploads, re-verifies, and only purges once **VERIFIED**. If it keeps failing,
check internet + token, and tell the PI (do **not** manually delete `data/` — that loses the night).

### 7.4 Disk getting full
Usually means uploads are failing and data is piling up. Fix the upload (§7.3). Never delete `data/`
by hand unless the PI confirms that night is already safely on ModelScope.

---

## 8. Mid-study maintenance

- **Cookie refresh (~every 2 weeks):** Douyin cookies expire. If you see rooms increasingly
  returning `error`/0 chat, redo §2.4 with a fresh browser cookie. Consider refreshing proactively
  around day 14.
- **Keep the machine awake:** verify sleep/suspend/hibernate are disabled; verify the clock is
  correct (NTP on) — the window timing depends on it.
- **Weekly:** glance at `df -h .` and that manifests exist for every night.

---

## 9. Quick reference

```bash
# setup (once)
git clone git@github.com:wangruosi/DouyinBarrage.git && cd DouyinBarrage
git checkout feat/timeline-sidecar
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
# ... cookie.txt, rooms.txt, clone dataset -> REPO, edit fleet/station.env ...
scripts/record.sh --minutes 3 && fleet/postrun.sh     # acceptance test

# run a night (manual; launch before START_AT)
source .venv/bin/activate
nohup fleet/nightly.sh > runs/nightly_$(date +%Y%m%d).out 2>&1 &

# each morning
pgrep -x ffmpeg | wc -l                                # 0 = idle
tail -n 30 logs/nightly-$(date -d yesterday +%Y%m%d).log
cat archive/manifest/$(date -d yesterday +%Y%m%d)/st01.json

# stop now (graceful)
kill -INT $(pgrep -f 'main\.py --all' | tail -1)
```

Escalate to the PI on: no manifest for a night, repeated upload failures, disk < 40 GB, or a wave
of `error` rooms (cookie).
```
