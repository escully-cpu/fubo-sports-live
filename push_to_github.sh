#!/bin/bash
# Runs every day at 9:05 AM via LaunchAgent (after update_local.py and
# auto_update.py have both had a chance to write index.html).
# Pushes the latest index.html to GitHub so the coworker-facing
# GitHub Pages version stays current.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

LOCK="/tmp/fubo_sports_index.lock"
# Wait up to 10 min for either writer (update_local.py / auto_update.py)
# to release the lock, so we never read the file mid-write.
for i in $(seq 1 120); do
  if mkdir "$LOCK" 2>/dev/null; then
    break
  fi
  sleep 5
done
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# ── Safety gate: refuse to commit/push a corrupted or truncated file ───────
# Guards against any race condition or crash leaving index.html empty or
# partially written (this happened once — an empty file got committed
# locally, though the push itself luckily failed before reaching GitHub).
BYTES=$(wc -c < index.html | tr -d ' ')
ITEM_COUNT=$(grep -o 'class="item' index.html | wc -l | tr -d ' ')

if [ "$BYTES" -lt 20000 ] || ! grep -q "</html>" index.html || [ "$ITEM_COUNT" -lt 50 ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ABORT — index.html looks corrupted (bytes=$BYTES items=$ITEM_COUNT). Not committing/pushing." >> "$DIR/logs/push_error.log"
  osascript -e 'display notification "index.html failed integrity check — daily push skipped, needs manual review" with title "fubo Calendar — ABORTED" sound name "Basso"' 2>/dev/null
  exit 1
fi

git add index.html
git diff --cached --quiet && exit 0   # nothing changed, skip push

git commit -m "Daily update — $(date '+%Y-%m-%d')"
git push origin main >> "$DIR/logs/push.log" 2>> "$DIR/logs/push_error.log"
