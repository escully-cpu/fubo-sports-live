#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
LOCK="/tmp/fubo_sports_index.lock"

# Wait up to 10 min for any other writer (update_local.py) to finish —
# mkdir is atomic, so this is safe even if two agents fire at once.
for i in $(seq 1 120); do
  if mkdir "$LOCK" 2>/dev/null; then
    break
  fi
  sleep 5
done
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

/usr/bin/python3 "$DIR/auto_update.py" >> "$DIR/logs/auto_update.log" 2>> "$DIR/logs/auto_update_error.log"
