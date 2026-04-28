"""
Daily update script — called by GitHub Actions every morning.
Reads the current index.html, asks Claude to refresh it (remove stale events,
highlight upcoming ones, update the timestamp), and writes the result back.
"""
import os
import re
import sys
from datetime import date
import anthropic

INDEX = "index.html"
today = date.today()

with open(INDEX, "r") as f:
    current_html = f.read()

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

prompt = f"""Today is {today.strftime('%B %d, %Y')}.

You are maintaining a live FuboTV sports events page. Below is the current HTML.

Your job:
1. Remove any event entries whose date has passed MORE THAN 14 DAYS AGO (keep recent ones — they may still be relevant for recap/context).
2. Leave all future and current events exactly as-is (keep all HTML structure, classes, styles).
3. Update the hero subtitle if it references a date range that needs refreshing (e.g. "Now thru Oct 2026").
4. Do NOT change any CSS, JavaScript, layout, or anything else — only remove stale event <div class="item ..."> blocks and update the hero text if needed.
5. Return ONLY the complete updated HTML document, nothing else — no markdown fences, no explanation.

Current HTML:
{current_html}
"""

message = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=8192,
    messages=[{"role": "user", "content": prompt}],
)

updated_html = message.content[0].text.strip()

# Sanity check: must look like an HTML document
if not updated_html.startswith("<!DOCTYPE") and not updated_html.startswith("<html"):
    print("ERROR: Response does not look like HTML. Aborting.", file=sys.stderr)
    print(updated_html[:500], file=sys.stderr)
    sys.exit(1)

with open(INDEX, "w") as f:
    f.write(updated_html)

print(f"Page updated for {today}.")
