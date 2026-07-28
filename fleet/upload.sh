#!/usr/bin/env bash
# fleet/upload.sh — commit this station-night's artifacts and push to the douyin dataset,
# safe under concurrent pushes from multiple stations (disjoint {station} paths + rebase retry).
#
# Usage:
#   fleet/upload.sh --repo /path/to/douyin --station st01 --date 20260727 [--branch master] [--retries 6]
#
# Pushes to master by default (ModelScope's dataset web UI only renders the master branch).
#
# Assumes fleet/pack.py already wrote video/text/manifest under $repo.
# Verifies each shard + text bundle exists on the remote (via git lfs ls-files) after push.
set -euo pipefail
REPO=""; STATION=""; DATE=""; BRANCH="master"; RETRIES=6
while [ $# -gt 0 ]; do case "$1" in
  --repo) REPO="$2"; shift 2;;
  --station) STATION="$2"; shift 2;;
  --date) DATE="$2"; shift 2;;
  --branch) BRANCH="$2"; shift 2;;
  --retries) RETRIES="$2"; shift 2;;
  *) echo "unknown arg: $1" >&2; exit 2;;
esac; done
[ -n "$REPO" ] && [ -n "$STATION" ] && [ -n "$DATE" ] || { echo "need --repo --station --date" >&2; exit 2; }
cd "$REPO"

# CRITICAL for a shared dataset: never download other stations' LFS blobs on pull/rebase.
# The remote accumulates thousands of tars over 30 nights; without this a single upload
# would try to smudge all of them. We only push our own freshly-packed tars (push side is
# unaffected by skip-smudge). Pulls fetch pointers only.
export GIT_LFS_SKIP_SMUDGE=1
git config --local lfs.fetchexclude "*" 2>/dev/null || true

# only ever stage this station's own paths -> pushes from other stations never conflict on content
PATHS=(
  "video/$DATE/$STATION"
  "text/$DATE/$STATION.tar.gz"
  "manifest/$DATE/$STATION.json"
)
git add -- "${PATHS[@]}" 2>/dev/null || true
if git diff --cached --quiet; then
  echo "[upload] nothing staged for $STATION $DATE — aborting"; exit 1
fi
git commit -q -m "$STATION $DATE: $(git diff --cached --name-only | wc -l | tr -d ' ') files"
echo "[upload] committed $(git rev-parse --short HEAD)"

# push with pull --rebase retry loop (disjoint paths => rebases auto-merge)
n=0
until git push origin "HEAD:$BRANCH" 2>&1 | tee /tmp/push.$$.log; do
  n=$((n+1)); [ "$n" -ge "$RETRIES" ] && { echo "[upload] push failed after $RETRIES tries"; exit 1; }
  sleep $(( (RANDOM % 5) + 2 ))
  echo "[upload] push race, rebasing (try $n)..."
  git pull --rebase origin "$BRANCH" || true
done
echo "[upload] pushed to $BRANCH"

# ---- verify: every packed tar is present as an LFS object on the remote branch ----
echo "[upload] verifying remote LFS objects on $BRANCH..."
git fetch -q origin "$BRANCH"
remote_lfs="$(git lfs ls-files -l --all 2>/dev/null | awk '{print $1"  "$3}')"
ok=1
while IFS= read -r f; do
  [ -z "$f" ] && continue
  base="$(basename "$f")"
  if echo "$remote_lfs" | grep -q -- "$base"; then
    echo "  OK   $base"
  else
    echo "  MISS $base"; ok=0
  fi
done < <(git show --pretty="" --name-only HEAD | grep -E '\.tar(\.gz)?$' || true)
[ "$ok" -eq 1 ] && echo "[upload] VERIFIED" || { echo "[upload] VERIFY FAILED"; exit 1; }
