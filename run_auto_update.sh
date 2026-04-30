#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
/usr/bin/python3 "$DIR/auto_update.py" >> "$DIR/logs/auto_update.log" 2>> "$DIR/logs/auto_update_error.log"
