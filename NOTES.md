# Timeline-sync additions

Fork of **[xhynice/DouyinBarrage](https://github.com/xhynice/DouyinBarrage)**
(upstream base commit `9a69921`). These notes describe the changes added on top of
upstream so they are easy to review and attribute.

## Overview

Two capabilities were added on top of upstream:

- **Part I — Timestamp alignment:** map every chat / like / social / stats row to the exact
  position in the recorded video, robustly across reconnects (gaps *and* GOP-cache overlaps).
- **Part II — Fighting data loss:** minimize loss at shutdown and on a throttled network, and
  make any loss that still happens *explicit* rather than silent.

| File | Change | Kind |
|------|--------|------|
| `service/recorder.py` | Timeline sidecar `timing_{anchor}.csv` (Part I) | modified (additive) |
| `service/fetcher.py`  | Auto-tag data → `*_aligned.csv` at session end (Part I) | modified (+10 lines) |
| `align.py`            | Alignment tool + `tag_all()` used by the app (Part I) | new file |
| `scripts/record.sh`   | Graceful timed stop + direct-recording default (Part II) | new file |
| `scripts/probe_net.py`| Direct-vs-proxy bandwidth probe (Part II evidence) | new file |

---

## Part I — Timestamp alignment

### The problem: naive mapping drifts

The data side is already clean: chat/like/social/stats `time` is **client wall-clock at receive**
(`strftime` when each WebSocket message is parsed), one continuous second-precision timeline.
That is the reference clock.

The **video** does not carry wall-clock. ffmpeg records with `-c copy`, so each segment's PTS
starts near 0, and the video and chat WebSockets reconnect *independently*. Reconnects create:

- **hidden intra-file gaps** — ffmpeg's own `-reconnect` keeps writing the *same* file, leaving a
  gap inside it (no new file);
- **overlaps** — on a full restart the CDN replays its buffered GOP cache, so consecutive segments
  duplicate a few seconds.

So `video_length ≠ wall-clock elapsed`, and filenames are only minute-precision. A 2 h / 13-room
run showed per-room segment sums ranging from **−110 s to +45 s** vs the 120-min window — i.e. no
fixed offset from video-time to wall-time. Filename + length cannot align reliably.

### The sidecar (`service/recorder.py`)

The recorder emits ffmpeg `-progress` and stamps each block with wall-clock time, writing one CSV
per session (spanning all segments/reconnects of that room):

```
timing_{anchor}.csv
  wall_epoch,        wall_iso,                 segment_file,                    video_pts_s
  1785076502.040,    2026-07-26 22:35:02.040,  与辉同行_20260726_2235_812.ts,   1.480
```

Why `out_time` (the muxed video position) and not wall-elapsed: during a reconnect gap `out_time`
**freezes** while wall-clock advances; on GOP-cache catch-up it **jumps**. Stamping that with
wall-clock captures the true, piecewise mapping — including the intra-file gaps a filename method
can't see. The video itself stays `-c copy`: **byte-identical, no re-encode**, ~1 parsed line/sec
of overhead.

### Auto-tagging at session end (`service/fetcher.py`)

At `stop()` — after the CSV/SQLite buffers flush and ts→mp4 conversion finishes — the fetcher calls
`align.tag_all()`, which writes `chat_aligned.csv`, `like_aligned.csv`, etc. Each adds three columns
to the originals:

```
… , segment_file, video_pts_s, in_gap
```

It no-ops when recording is disabled (no timing file present). Nothing else in the pipeline changes.

### Manual tool (`align.py`)

```bash
python align.py tag   data/<anchor>/<session> chat     # or like/social/stats
python align.py at    data/<anchor>/<session> "2026-07-26 22:54:34"
python align.py frame data/<anchor>/<session> "2026-07-26 22:54:34" out.jpg
```

`align.py` normalizes each segment to 0-based (`video_pts_s − segment's first pts`), because the
converted `.mp4` is zero-based while the raw mpegts `out_time` carries the stream's PTS offset — so
the reported `video_pts_s` is the correct `-ss` seek into the `.mp4`.

### Precision

Map resolution is ~1 s (the sample period) with linear interpolation between samples. The dominant
error is upstream of us: chat `time` is receive-time with ~0.5–2 s WebSocket-batching jitter. Net
end-to-end alignment is ±1–2 s — fine for "what was chat saying at this video moment". Frame-exact
sync would need a wall-clock burn-in overlay (a re-encoding add-on, not currently enabled).

---

## Part II — Fighting data loss

### Inherited from upstream (not ours)

The core streaming resilience is upstream: ffmpeg `-reconnect_streamed -reconnect_at_eof
-reconnect_delay_max 60`, the business watchdog that re-establishes dropped WebSocket/record
connections, and startup recovery of orphaned `/tmp` files. The fork does not modify that engine —
it adds the three measures below.

### 1. Graceful timed stop (`scripts/record.sh`)

Timed runs stop with **`timeout --signal=INT --kill-after=N`**, i.e. SIGINT, not a hard kill. SIGINT
routes into the app's shutdown path, which:

- flushes the in-memory barrage buffer (the background flush is every 10 s, so a hard kill would lose
  **up to ~10 s** of chat/like/social/stats rows),
- closes the timing sidecar,
- converts the `.ts` → `.mp4`,
- runs auto-tagging.

`--kill-after=N` only force-kills if graceful shutdown hangs. A plain `SIGTERM`/`SIGKILL` would skip
all four steps.

### 2. Direct-recording default (`scripts/record.sh --direct`)

A live FLV is delivered at roughly its encoding bitrate; if the pull can't keep up **in real time**,
frames are dropped — you can't "catch up" on a live stream. Measured on this network with
`scripts/probe_net.py`:

| Path | Throughput (标清 stream) |
|------|--------------------------|
| via proxy | **0.26 Mbps** — below 标清's ~1 Mbps bitrate → drops |
| direct    | **1.47 Mbps** — ≈ real-time → no lag |

So `record.sh` defaults to `--direct` (strips the proxy env for API + WebSocket + ffmpeg). Aggregate
headroom is ample: 13 rooms at 原画 sustained **76 Mbps**, and a 39-connection push reached
**211 Mbps** without saturating — bandwidth is not the constraint for direct recording.

### 3. Loss visibility (`in_gap`)

When a reconnect *does* drop a slice, the sidecar makes it explicit: a data row whose wall-clock lands
inside a frozen-`out_time` interval is tagged **`in_gap=True`** (and a moment outside all coverage →
`outside`). You never silently mis-align chat onto the wrong frame.

### Evidence (real 2 h / 13-room run)

- **0 collection errors**, **155 reconnects absorbed**, full **118–120 min** coverage on every room.
- Reconnect seams produced both gaps and overlaps (e.g. 九度七 5 segments, −110 s; DPU +45 s; 满城红
  +39 s; 与辉同行 1 segment, −2 s) — all reconciled by the sidecar.
- Live spot-check: chat at `22:54:34` → `align.py frame` → video `@ 322.818 s`, showing the correct
  host/scene.

---

## Quick reference

```bash
# record (env + direct default + graceful timed stop)
scripts/record.sh --minutes 120 --at 18:30      # scheduled 2 h run, all rooms
scripts/record.sh --minutes 10 --room <id>      # 10-min single room
# align (also runs automatically at session end)
python align.py tag data/<anchor>/<session> chat
python align.py frame data/<anchor>/<session> "<YYYY-MM-DD HH:MM:SS>" out.jpg
# per-room health: segments, break durations, in_gap/outside counts (one session or all of data/)
python align.py summary data/
# diagnose the network
python scripts/probe_net.py <room_id>
```

| Artifact | Written by | Contents |
|----------|-----------|----------|
| `data/<anchor>/<session>/timing_*.csv` | recorder | wall-clock ↔ video-position map |
| `data/<anchor>/<session>/*_aligned.csv` | fetcher (auto) | data rows + `segment_file, video_pts_s, in_gap` |
| `runs/<timestamp>.log` | `record.sh` | wrapper run log (gitignored) |

## Upstream vs added

| Concern | Upstream | This fork |
|---------|----------|-----------|
| Reconnect / retry engine | ✅ ffmpeg `-reconnect`, watchdog, `/tmp` recovery | — |
| Video↔data timeline | — | ✅ sidecar + auto-tag + `align.py` |
| Graceful stop / no-buffer-loss | — | ✅ SIGINT timed stop in `record.sh` |
| Real-time pull (no drop) | — | ✅ direct-recording default + `probe_net.py` |
| Loss made explicit | — | ✅ `in_gap` flag |

## Publishing this as your fork

```bash
git remote add fork git@github.com:<you>/DouyinBarrage.git
git push fork feat/timeline-sidecar
```

The changes also live in `patches/timeline-sidecar.patch` (re-appliable with `git apply`).

## Not committed (local only)

`rooms.txt` (room list), `rooms.all.txt`, `runs/`, `.venv/`, `data/`, `logs/`, `cookie.txt`,
`*.log` — environment/config/secrets, not part of the feature.
