#!/bin/bash
# Called by launchd every Sunday at 9:30 AM
DIR="$(cd "$(dirname "$0")" && pwd)"
/usr/bin/python3 "$DIR/weekly_audit.py" >> "$DIR/logs/audit.log" 2>> "$DIR/logs/audit_error.log"
