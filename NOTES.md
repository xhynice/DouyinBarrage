# Timeline-sync additions

Fork of **[xhynice/DouyinBarrage](https://github.com/xhynice/DouyinBarrage)**
(upstream base commit `9a69921`). These notes describe the changes added on top of
upstream so they are easy to review and attribute.

## What was added

A **wall-clock ↔ video-position timeline** so chat / like / social / stats data can be
aligned to the recorded video, robustly across reconnects (gaps *and* GOP-cache overlaps).

| File | Change | Kind |
|------|--------|------|
| `service/recorder.py` | Writes a `timing_{anchor}.csv` sidecar per session: `wall_epoch, wall_iso, segment_file, video_pts_s`, sampled ~1×/s from ffmpeg `-progress out_time`. Video stays `-c copy` (byte-identical, no re-encode). | modified (additive) |
| `service/fetcher.py` | At `stop()` (session end), auto-runs `align.tag_all()` → emits `*_aligned.csv`. No-ops when recording is disabled (no timing file). | modified (+10 lines) |
| `align.py` | New standalone tool: `tag` / `at` / `frame` CLI + `tag_all()` used by the app. Per-segment zero-base normalization so seeks land correctly in the `.mp4`. | new file |

## Why the sidecar (not filename + length)

Reconnects create **hidden intra-file gaps** (ffmpeg's own `-reconnect` keeps writing the
same file) and **overlaps** (the CDN replays its buffered GOP on restart). So video length
≠ wall-clock elapsed, and filenames are only minute-precision. `-progress out_time` freezes
during a gap and jumps on catch-up, so stamping it with wall-clock captures the true mapping.

## Usage

```bash
# tagging runs automatically at session end. Manual use:
python align.py tag   data/<anchor>/<session> chat        # or like/social/stats
python align.py at    data/<anchor>/<session> "2026-07-26 22:54:34"
python align.py frame data/<anchor>/<session> "2026-07-26 22:54:34" out.jpg
```

`*_aligned.csv` = original columns + `segment_file, video_pts_s, in_gap`.

## Publishing this as your fork

```bash
# 1) create your fork on GitHub (needs your auth), then:
git remote add fork https://github.com/<you>/DouyinBarrage.git
git push fork feat/timeline-sidecar
# open a self-PR main <- feat/timeline-sidecar on your fork to see the clean diff
```

The changes also live in `patches/timeline-sidecar.patch` (re-appliable with `git apply`).

## Not committed (local only)

`config.yaml` (quality/direct settings), `rooms.txt` (room list), `run_*.sh`, `probe_net.py`,
`.venv/`, `data/`, `*.log` — environment/config, not part of the feature.
