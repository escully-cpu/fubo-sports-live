#!/bin/bash
# Called by launchd every morning at 9 AM
DIR="$(cd "$(dirname "$0")" && pwd)"
/usr/bin/python3 "$DIR/update_local.py" >> "$DIR/logs/update.log" 2>> "$DIR/logs/update_error.log"
