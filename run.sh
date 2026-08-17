#!/bin/bash
# Called by launchd every morning at 9 AM
DIR="$(cd "$(dirname "$0")" && pwd)"
LOCK="/tmp/fubo_sports_index.lock"

# Wait up to 10 min for any other writer (auto_update.py) to finish —
# mkdir is atomic, so this is safe even if two agents fire at once.
for i in $(seq 1 120); do
  if mkdir "$LOCK" 2>/dev/null; then
    break
  fi
  sleep 5
done
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

/usr/bin/python3 "$DIR/update_local.py" >> "$DIR/logs/update.log" 2>> "$DIR/logs/update_error.log"
