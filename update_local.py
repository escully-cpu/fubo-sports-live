"""
Runs every morning at 9 AM via macOS LaunchAgent.
Strips events whose start date is more than 14 days past.
No API key, no internet — pure date math.
"""
import re
import sys
from datetime import date, timedelta
from bs4 import BeautifulSoup

INDEX = "/Users/emmascully/projects/fubo-sports-live/index.html"
CUTOFF_DAYS = 14  # keep events up to 14 days after they start

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

def parse_start_date(text, year=2026):
    """
    Handles every date format used in the HTML:
      May 10           → May 10
      May 14–17        → May 14  (range: take start)
      May 24–Jun 7     → May 24  (cross-month range: take start)
      May 19+          → May 19
      May (ongoing)    → May 1
      May (weekly)     → May 1
      Jun (TBD)        → Jun 1
      Sep (Finals week)→ Sep 1
      Late Oct (TBD)   → Oct 20
    """
    t = text.strip().lstrip('~').strip()

    # "Late Month ..." → mid-to-late month estimate
    m = re.match(r"late\s+([A-Za-z]+)", t, re.IGNORECASE)
    if m:
        mo = MONTHS.get(m.group(1).lower()[:3])
        if mo:
            return date(year, mo, 20)

    # "Month Day" (catches ranges, + suffix, cross-month — just grab first number)
    m = re.match(r"([A-Za-z]+)[^\d]*(\d+)", t)
    if m:
        mo = MONTHS.get(m.group(1).lower()[:3])
        if mo:
            return date(year, mo, int(m.group(2)))

    # Month-only: "(TBD)", "(weekly)", "(ongoing)", "(Finals week)", etc.
    m = re.match(r"([A-Za-z]+)", t)
    if m:
        mo = MONTHS.get(m.group(1).lower()[:3])
        if mo:
            return date(year, mo, 1)

    return None


def main():
    today = date.today()
    cutoff = today - timedelta(days=CUTOFF_DAYS)

    with open(INDEX, encoding="utf-8") as f:
        html = f.read()

    soup = BeautifulSoup(html, "html.parser")

    removed = 0
    for item in soup.find_all("div", class_="item"):
        date_div = item.find("div", class_="date")
        if not date_div:
            continue
        start = parse_start_date(date_div.get_text())
        if start and start < cutoff:
            item.decompose()
            removed += 1

    # Remove any month-block whose .list div is now empty
    for block in soup.find_all("div", class_="month-block"):
        event_list = block.find("div", class_="list")
        if event_list and not event_list.find("div", class_="item"):
            block.decompose()

    with open(INDEX, "w", encoding="utf-8") as f:
        f.write(str(soup))

    print(f"[{today}] Done — removed {removed} stale event(s).", flush=True)


if __name__ == "__main__":
    main()
