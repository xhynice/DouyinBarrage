#!/usr/bin/env bash
# fleet/install_cron.sh — install (or refresh) the nightly cron entry for THIS station. Idempotent.
#
#   fleet/install_cron.sh          # install/refresh
#   fleet/install_cron.sh --show   # print current fleet cron lines
#   fleet/install_cron.sh --remove # remove the nightly entry
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/station.env"

NIGHTLY="$HERE/nightly.sh"
# fire cron a couple minutes BEFORE START_AT so record.sh's --at lands the window precisely
CRON_HH="${START_AT%%:*}"; CRON_MM="${START_AT##*:}"
CRON_MM=$((10#$CRON_MM - 2)); [ "$CRON_MM" -lt 0 ] && CRON_MM=$((CRON_MM + 60)) && CRON_HH=$((10#$CRON_HH - 1))
LINE="$CRON_MM $CRON_HH * * * $NIGHTLY"

case "${1:-}" in
  --show)   crontab -l 2>/dev/null | grep -F "$NIGHTLY" || echo "(no fleet cron installed)"; exit 0;;
  --remove) crontab -l 2>/dev/null | grep -vF "$NIGHTLY" | crontab - ; echo "removed"; exit 0;;
esac

chmod +x "$NIGHTLY" "$HERE/postrun.sh" 2>/dev/null || true
# replace any prior nightly entry, then add the fresh one
( crontab -l 2>/dev/null | grep -vF "$NIGHTLY" ; echo "$LINE" ) | crontab -
echo "installed cron for station $STATION:"
crontab -l | grep -F "$NIGHTLY"
echo "(fires $CRON_MM:$CRON_HH -> nightly.sh -> record.sh --at $START_AT --minutes $MINUTES -> postrun.sh)"
