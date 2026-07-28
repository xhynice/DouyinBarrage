#!/usr/bin/env bash
# fleet/postrun.sh — after recording: assert-idle -> pack -> upload+verify -> purge -> status.
#
# Purge is GATED on a verified upload: if upload fails, NOTHING local is deleted (retry next night).
# Kept forever locally: text bundle + manifest. Deleted after verify: raw video + LFS blob cache.
#
# Config comes from station.env (same dir). DATE/AFTER may be exported by nightly.sh; otherwise
# they default to today / 0000.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/station.env"

DATE="${DATE:-$(date +%Y%m%d)}"
AFTER="${AFTER:-0000}"
log() { echo "[postrun $(date '+%F %T')] $*"; }

# ---------- A. assert the recorder is gone (the 'is the station off?' check) ----------
if pgrep -f "python -u main.py" >/dev/null 2>&1; then
  log "WARN recorder still running after window — sending SIGINT"
  pkill -INT -f "python -u main.py" || true
  for _ in $(seq 1 30); do pgrep -f "python -u main.py" >/dev/null 2>&1 || break; sleep 2; done
fi
if pgrep -f "python -u main.py" >/dev/null 2>&1; then IDLE=false; else IDLE=true; fi
log "idle=$IDLE"

# ---------- A1. deferred ts->mp4 convert (moved off the timed stop path, run in parallel) ----------
# Recorder writes .ts (auto_convert disabled so timed stop is fast). Convert here, after the
# process is gone, in parallel across all segments — validate with ffprobe, then drop the .ts.
log "convert ts->mp4 ..."
python3 - "$DATA_DIR" "$DATE" <<'PY' || log "WARN convert step had issues (non-fatal)"
import sys, glob, os, subprocess, concurrent.futures as cf
data, date = sys.argv[1], sys.argv[2]
ts_files = sorted(glob.glob(f"{data}/*/{date}_*/*.ts"))
def conv(ts):
    mp4 = ts[:-3] + ".mp4"
    if os.path.exists(mp4) and os.path.getsize(mp4) > 0:
        if os.path.exists(ts): os.remove(ts)
        return "skip"
    if not os.path.exists(ts) or os.path.getsize(ts) == 0:
        return "empty"
    r = subprocess.run(["ffmpeg","-y","-v","error","-i",ts,"-c","copy",
                        "-movflags","+faststart","-f","mp4",mp4],
                       capture_output=True, timeout=600)
    if r.returncode != 0 or not os.path.exists(mp4) or os.path.getsize(mp4) == 0:
        if os.path.exists(mp4) and os.path.getsize(mp4) == 0: os.remove(mp4)
        return "fail"
    p = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                        "-of","csv=p=0",mp4], capture_output=True, timeout=30)
    if p.returncode != 0 or not p.stdout.strip():
        os.remove(mp4); return "badprobe"
    os.remove(ts); return "ok"
if not ts_files:
    print("[convert] no .ts (already mp4?)"); sys.exit(0)
ok = 0
with cf.ThreadPoolExecutor(max_workers=min(16, (os.cpu_count() or 4))) as ex:
    for res in ex.map(conv, ts_files):
        if res in ("ok","skip"): ok += 1
print(f"[convert] {ok}/{len(ts_files)} segments -> mp4")
PY

# ---------- A2. deferred alignment (moved off the timed stop path) ----------
# The recorder skips inline ts->video alignment during a timed stop (DOUYIN_DEFER_ALIGN=1) so it
# exits fast; we run it here, after the process is gone, with time to spare (no window deadline).
log "align tonight's sessions ..."
( cd "$APP_DIR" && python3 - "$DATA_DIR" "$DATE" <<'PY'
import sys, glob, os
sys.path.insert(0, os.getcwd())
data, date = sys.argv[1], sys.argv[2]
try:
    from align import tag_all
except Exception as e:
    print("[align] import failed:", e); sys.exit(0)
n = 0
for sess in sorted(glob.glob(f"{data}/*/{date}_*")):
    try:
        if tag_all(sess): n += 1
    except Exception as e:
        print("[align] skip", sess, e)
print(f"[align] aligned {n} sessions")
PY
) || log "WARN alignment step had issues (non-fatal)"

# ---------- B. pack tonight's sessions into shards + text bundle + manifest ----------
log "pack (station=$STATION date=$DATE after=$AFTER) ..."
if ! python3 "$HERE/pack.py" --station "$STATION" --date "$DATE" --after "$AFTER" \
      --data-dir "$DATA_DIR" --out-dir "$REPO" --shard-gb "$SHARD_GB"; then
  log "pack FAILED — aborting (nothing purged)"; exit 1
fi

# ---------- B2. archive text bundle locally (kept forever, independent of the push vehicle) ----------
# The repo is a disposable push vehicle whose LFS cache we later nuke; the local-forever text
# copy must live OUTSIDE it. Copy while the bundle is still real content (before any reclaim).
ARCHIVE="$APP_DIR/archive"
mkdir -p "$ARCHIVE/text/$DATE"
cp -f "$REPO/text/$DATE/$STATION.tar.gz" "$ARCHIVE/text/$DATE/$STATION.tar.gz" 2>/dev/null \
  && log "archived text -> $ARCHIVE/text/$DATE/$STATION.tar.gz" \
  || log "WARN could not archive text bundle"

# ---------- C. upload to master + verify remote LFS objects ----------
log "upload+verify -> $BRANCH ..."
if "$HERE/upload.sh" --repo "$REPO" --station "$STATION" --date "$DATE" --branch "$BRANCH"; then
  UPLOAD=verified
else
  UPLOAD=failed
fi
log "upload=$UPLOAD"

# ---------- record idle+upload into the manifest (also the morning brief) ----------
MANIFEST="$REPO/manifest/$DATE/$STATION.json"
python3 - "$MANIFEST" "$IDLE" "$UPLOAD" <<'PY' || true
import json, sys
path, idle, up = sys.argv[1], sys.argv[2], sys.argv[3]
m = json.load(open(path, encoding="utf-8"))
m["idle"] = (idle == "true"); m["upload"] = up
json.dump(m, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
# archive the finalized manifest locally too (tiny, plain json — the morning brief)
mkdir -p "$ARCHIVE/manifest/$DATE"
cp -f "$MANIFEST" "$ARCHIVE/manifest/$DATE/$STATION.json" 2>/dev/null || true

# ---------- D. purge — ONLY when verified ----------
if [ "$UPLOAD" = verified ]; then
  log "verified -> purging tonight's raw recordings + LFS blob cache (keeping text + manifest)"
  # 1) tonight's session dirs (video + csv) across all anchors; keep anchor-level meta.json
  before=$(du -sk "$DATA_DIR" 2>/dev/null | awk '{print int($1/1024)}')
  find "$DATA_DIR" -maxdepth 2 -type d -name "${DATE}_*" -exec rm -rf {} + 2>/dev/null || true
  # 2) reclaim the clone's local copies of the (now verified-on-remote) shards:
  #    working-tree tar files + the LFS blob cache. git-lfs prune keeps HEAD-referenced blobs,
  #    so we clear the object cache directly, then restore tiny pointers to keep the tree clean.
  lfs_before=$(du -sk "$REPO/.git/lfs" 2>/dev/null | awk '{print int($1/1024)}')
  rm -f "$REPO/video/$DATE/$STATION/"*.tar 2>/dev/null || true
  rm -rf "$REPO/.git/lfs/objects/"* 2>/dev/null || true
  ( cd "$REPO" && GIT_LFS_SKIP_SMUDGE=1 git checkout -q -- "video/$DATE/$STATION" 2>/dev/null || true )
  after=$(du -sk "$DATA_DIR" 2>/dev/null | awk '{print int($1/1024)}')
  lfs_after=$(du -sk "$REPO/.git/lfs" 2>/dev/null | awk '{print int($1/1024)}')
  log "purge done (data/ ${before}MB->${after}MB, .git/lfs ${lfs_before}MB->${lfs_after}MB)"
else
  log "NOT verified -> retaining ALL local data for retry"
fi

# ---------- E. disk guard + final status line (morning brief) ----------
FREE=$(df -Pk "$DATA_DIR" | awk 'NR==2{print int($4/1024/1024)}')
[ "$FREE" -lt "$DISK_FLOOR_GB" ] && log "WARN disk low: ${FREE}GB < floor ${DISK_FLOOR_GB}GB"
log "DONE station=$STATION date=$DATE idle=$IDLE upload=$UPLOAD disk_free=${FREE}GB"

# non-zero exit if something needs a human (not idle, or upload failed)
[ "$IDLE" = true ] && [ "$UPLOAD" = verified ]
