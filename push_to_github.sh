#!/bin/bash
# Runs every Monday at 9:05 AM via LaunchAgent.
# Pushes the latest index.html to GitHub so the coworker-facing
# GitHub Pages version stays current (weekly cadence).
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

git add index.html
git diff --cached --quiet && exit 0   # nothing changed, skip push

git commit -m "Weekly update — $(date '+%Y-%m-%d')"
git push origin main >> "$DIR/logs/push.log" 2>> "$DIR/logs/push_error.log"
