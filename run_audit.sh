#!/bin/bash
# Called by launchd every Sunday at 9:15 AM (weekly Deportes-coverage +
# cancellation/reschedule news scan — see weekly_audit.py)
DIR="$(cd "$(dirname "$0")" && pwd)"
LOCK="/tmp/fubo_sports_index.lock"

# Wait up to 10 min for any writer to finish so we read a consistent file.
for i in $(seq 1 120); do
  if mkdir "$LOCK" 2>/dev/null; then
    break
  fi
  sleep 5
done
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

/usr/bin/python3 "$DIR/weekly_audit.py" >> "$DIR/logs/audit.log" 2>> "$DIR/logs/audit_error.log"
