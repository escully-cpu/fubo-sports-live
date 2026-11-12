#!/bin/bash
# Autonomous fact-check of index.html. Usage: run_verify.sh [daily|weekly]
#   daily  — re-verify the next 45 days (0-14 days: skip cards verified in the last
#            3 days; 15-45 days: skip cards verified in the last 7 days)
#   weekly — re-verify the next 45 days fully, light-check 46-60 days out
#            (~ / TBD / single-source cards), and add newly announced events.
# Edits index.html in place via headless Claude, then lints. Lint failure or an
# oversized diff reverts the run. The daily 9:05 push (push_to_github.sh)
# publishes whatever survives.
MODE="${1:-weekly}"
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/logs/verify.log"
LOCK="/tmp/fubo_sports_index.lock"
TODAY="$(date +%Y-%m-%d)"

CLAUDE_BIN="$(ls -d "$HOME"/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null | sort -V | tail -1)"
[ -x "$CLAUDE_BIN" ] || CLAUDE_BIN="$(command -v claude)"
if [ ! -x "$CLAUDE_BIN" ]; then
  echo "[$TODAY] ABORT — claude binary not found" >> "$LOG"
  exit 1
fi

# One writer at a time (same lock as update_local / auto_update / push).
for i in $(seq 1 120); do
  mkdir "$LOCK" 2>/dev/null && break
  sleep 5
done
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

cd "$DIR" || exit 1
cp index.html "$DIR/logs/index.pre_verify.html"

if [ "$MODE" = "daily" ]; then
  SCOPE="Verify every card dated within the next 45 days (today through $(date -v+45d +%Y-%m-%d)). Skip a card if its data-verified is within the last 3 days (cards dated 0-14 days out) or within the last 7 days (cards dated 15-45 days out), unless it has a '~' date, no airtime or a 'confirm' note. Work nearest-dated first: cards airing in the next 3 days, then the rest of the first 14 days, then 15-45 days out. Do not look further out than 45 days and do not add new events."
else
  SCOPE="Verify EVERY card dated within the next 45 days (today through $(date -v+45d +%Y-%m-%d)), regardless of when it was last verified. For cards 46-60 days out (through $(date -v+60d +%Y-%m-%d)), verify only those with a '~' date, a TBD/approximate date, no airtime, a 'confirm' note, or no data-verified stamp."
fi
PROMPT="$(sed -e "s|{TODAY}|$TODAY|g" -e "s|{MODE}|$MODE|g" -e "s|{SCOPE}|$SCOPE|g" "$DIR/verify_prompt.md")"

echo "=== [$(date '+%Y-%m-%d %H:%M:%S')] verify ($MODE) start" >> "$LOG"
"$CLAUDE_BIN" -p "$PROMPT" \
  --allowedTools "WebSearch,Read,Edit" \
  --permission-mode acceptEdits \
  --max-turns 400 \
  >> "$LOG" 2>&1

# ── Safety checks: revert the whole run if anything looks wrong ────────────
revert() {
  cp "$DIR/logs/index.pre_verify.html" index.html
  echo "[$(date '+%H:%M:%S')] REVERTED: $1" >> "$LOG"
  exit 1
}
BYTES=$(wc -c < index.html | tr -d ' ')
ITEMS_BEFORE=$(grep -o 'class="item' "$DIR/logs/index.pre_verify.html" | wc -l | tr -d ' ')
ITEMS_AFTER=$(grep -o 'class="item' index.html | wc -l | tr -d ' ')
[ "$BYTES" -lt 20000 ] || ! grep -q "</html>" index.html && revert "index.html looks corrupted"
[ "$ITEMS_AFTER" -lt $((ITEMS_BEFORE - 8)) ] && revert "removed too many cards ($ITEMS_BEFORE -> $ITEMS_AFTER)"
CHANGED=$(diff "$DIR/logs/index.pre_verify.html" index.html | grep -c '^[<>]')
[ "$CHANGED" -gt 400 ] && revert "diff too large ($CHANGED lines)"
# CSS/JS must be untouched: compare everything before the first month block
H1=$(sed -n '1,/<div class="columns-wrapper">/p' "$DIR/logs/index.pre_verify.html" | shasum)
H2=$(sed -n '1,/<div class="columns-wrapper">/p' index.html | shasum)
T1=$(sed -n '/<!-- end columns-wrapper -->/,$p' "$DIR/logs/index.pre_verify.html" | shasum)
T2=$(sed -n '/<!-- end columns-wrapper -->/,$p' index.html | shasum)
[ "$H1" != "$H2" ] || [ "$T1" != "$T2" ] && revert "page header/CSS/JS was modified"
/usr/bin/python3 "$DIR/lint_calendar.py" index.html >> "$LOG" 2>&1 || revert "lint errors after verification"

echo "=== [$(date '+%Y-%m-%d %H:%M:%S')] verify ($MODE) done: $CHANGED changed lines, $ITEMS_BEFORE -> $ITEMS_AFTER cards" >> "$LOG"
