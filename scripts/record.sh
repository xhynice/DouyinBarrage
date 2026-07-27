#!/usr/bin/env bash
# DouyinBarrage recorder wrapper — env setup + optional scheduled start + timed graceful stop.
#
# Replaces the old run.sh / run_test_10min.sh / run_direct_30min.sh / run_all_2h.sh.
#
# Usage:
#   scripts/record.sh [options] [-- <extra main.py args>]
#
# Options:
#   --minutes N     stop gracefully after N minutes (0 = run until Ctrl-C). default 0
#   --at HH:MM      wait until this clock time before starting (today)
#   --room ID       record a single room ID (default: --all from rooms.txt)
#   --proxy         route through the proxy env (default: --direct, proxy stripped)
#   --direct        force direct (no proxy)  [default]
#   --no-record     collect chat/data only, no video recording
#   --log-level L   DEBUG/INFO/WARNING/ERROR/NONE (default INFO)
#
# Examples:
#   scripts/record.sh                          # run until Ctrl-C, all rooms, record, direct
#   scripts/record.sh --minutes 10             # 10-min timed run
#   scripts/record.sh --minutes 30             # 30-min timed run
#   scripts/record.sh --minutes 120 --at 18:30 # wait until 18:30, then run 2h
#   scripts/record.sh --minutes 5 --room 56697889278   # single room, 5 min
set -euo pipefail

# repo dir = parent of this script's dir (portable — no hardcoded absolute path)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

MINUTES=0; AT=""; ROOM=""; NET="direct"; RECORD=1; LOGLEVEL="INFO"
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --minutes)   MINUTES="$2"; shift 2;;
    --at)        AT="$2"; shift 2;;
    --room)      ROOM="$2"; shift 2;;
    --proxy)     NET="proxy"; shift;;
    --direct)    NET="direct"; shift;;
    --no-record) RECORD=0; shift;;
    --log-level) LOGLEVEL="$2"; shift 2;;
    --)          shift; EXTRA=("$@"); break;;
    *) echo "unknown arg: $1  (see header for usage)" >&2; exit 2;;
  esac
done

# ffmpeg on PATH? otherwise try common local installs (harmless if absent)
if ! command -v ffmpeg >/dev/null 2>&1; then
  for d in "$HOME/.local/bin" "$HOME/fsl/bin"; do
    [ -x "$d/ffmpeg" ] && { export PATH="$d:$PATH"; break; }
  done
fi

# network mode
if [ "$NET" = "direct" ]; then
  unset HTTP_PROXY HTTPS_PROXY ALL_PROXY WS_PROXY WSS_PROXY \
        http_proxy https_proxy all_proxy no_proxy 2>/dev/null || true
else
  export http_proxy="${HTTP_PROXY:-}" https_proxy="${HTTPS_PROXY:-}"   # ffmpeg reads lowercase
fi

# python venv, if present
[ -f .venv/bin/activate ] && source .venv/bin/activate

# recording control:
#   default / --record  -> force-enable (pass --record; deterministic regardless of config)
#   --no-record         -> force-disable by temporarily flipping config.yaml's record.enabled
#                          (comment-preserving sed on the record: block, restored on exit)
_CFG_BAK=""
restore_cfg() { [ -n "$_CFG_BAK" ] && [ -f "$_CFG_BAK" ] && mv -f "$_CFG_BAK" config.yaml; }
if [ "$RECORD" -eq 0 ] && [ -f config.yaml ]; then
  _CFG_BAK="$(mktemp)"; cp config.yaml "$_CFG_BAK"
  trap restore_cfg EXIT INT TERM
  sed -i '/^record:/,/^[a-zA-Z]/ s/^\(  enabled:\) *true/\1 false/' config.yaml
fi

# optional scheduled start
if [ -n "$AT" ]; then
  target=$(date -d "$AT" +%s); now=$(date +%s); delay=$(( target - now ))
  if [ "$delay" -gt 0 ]; then
    echo "waiting ${delay}s until $(date -d @"$target" '+%F %T') to start..."
    sleep "$delay"
  fi
fi

# assemble main.py command
if [ -n "$ROOM" ]; then TARGET=("$ROOM"); else TARGET=(--all); fi
CMD=(python -u main.py "${TARGET[@]}" --log-level "$LOGLEVEL")
[ "$RECORD" -eq 1 ] && CMD+=(--record)
[ "${#EXTRA[@]}" -gt 0 ] && CMD+=("${EXTRA[@]}")

# tee output to runs/<timestamp>.log
mkdir -p runs
LOG="runs/$(date +%Y%m%d_%H%M%S).log"
echo "START $(date '+%F %T')  net=$NET record=$RECORD minutes=$MINUTES  -> $LOG"
echo "  ${CMD[*]}"

if [ "$MINUTES" -gt 0 ]; then
  # SIGINT for graceful stop (flush CSV/SQLite, close timing sidecar, ts->mp4, auto-tag);
  # force-kill 300s later if it hangs.
  timeout --signal=INT --kill-after=300 "$(( MINUTES * 60 ))" "${CMD[@]}" 2>&1 | tee "$LOG"
else
  "${CMD[@]}" 2>&1 | tee "$LOG"
fi
echo "END $(date '+%F %T')"
