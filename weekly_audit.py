"""
Weekly calendar audit — runs every Sunday at 9:30 AM via LaunchAgent.
Scans entertainment/sports RSS feeds and Google News for any signs that
calendar events have been cancelled, postponed, or rescheduled.
No API keys. Completely free.
"""

import re
import sys
import time
import subprocess
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

sys.path.insert(0, "/Users/emmascully/Library/Python/3.9/lib/python/site-packages")
import requests
from bs4 import BeautifulSoup

INDEX    = "/Users/emmascully/projects/fubo-sports-live/index.html"
LOGS_DIR = "/Users/emmascully/projects/fubo-sports-live/logs"

LOOKBACK_DAYS  = 8    # news published in the last 8 days
UPCOMING_DAYS  = 60   # run targeted searches for events within 60 days

# Free RSS feeds — script skips any that fail gracefully
RSS_FEEDS = [
    # Entertainment news — confirmed working
    ("Deadline",             "https://deadline.com/feed/"),
    ("Variety",              "https://variety.com/feed/"),
    ("TVLine",               "https://tvline.com/feed/"),
    ("Hollywood Reporter",   "https://www.hollywoodreporter.com/feed/"),
    # Network press rooms — confirmed working
    ("ESPN Press",           "https://espnpressroom.com/us/feed/"),
    # Google News broad sweeps
    ("Google News — TV",     "https://news.google.com/rss/search?q=TV+show+cancelled+OR+postponed+OR+renewed+2026&hl=en-US&gl=US&ceid=US:en"),
    ("Google News — Sports", "https://news.google.com/rss/search?q=sports+event+cancelled+OR+postponed+OR+rescheduled+2026&hl=en-US&gl=US&ceid=US:en"),
    ("Google News — Awards", "https://news.google.com/rss/search?q=awards+show+cancelled+OR+postponed+OR+%22date+change%22+2026&hl=en-US&gl=US&ceid=US:en"),
]

# Headlines containing any of these words near a show name trigger a flag
FLAG_WORDS = [
    "cancel", "cancell", "postpone", "delay", "reschedule", "push back",
    "pulled", "axed", "hiatus", "not returning", "no longer",
    "date change", "new date", "moved to", "premiere date changed",
    "ending", "final season", "last season", "series finale",
    "production halt", "shut down", "won't return", "will not return",
]

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_start_date(text, year=2026):
    t = text.strip().lstrip("~").strip()
    m = re.match(r"late\s+([A-Za-z]+)", t, re.IGNORECASE)
    if m:
        mo = MONTHS.get(m.group(1).lower()[:3])
        if mo:
            return date(year, mo, 20)
    m = re.match(r"([A-Za-z]+)[^\d]*(\d+)", t)
    if m:
        mo = MONTHS.get(m.group(1).lower()[:3])
        if mo:
            return date(year, mo, int(m.group(2)))
    m = re.match(r"([A-Za-z]+)", t)
    if m:
        mo = MONTHS.get(m.group(1).lower()[:3])
        if mo:
            return date(year, mo, 1)
    return None


def fetch_rss(name, url):
    """Fetch recent RSS items. Returns [] on any error."""
    cutoff = datetime.now() - timedelta(days=LOOKBACK_DAYS)
    items  = []
    try:
        r = requests.get(url, timeout=14,
                         headers={"User-Agent": "Mozilla/5.0 (Macintosh)"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            pub   = (item.findtext("pubDate") or "")
            try:
                dt = parsedate_to_datetime(pub).replace(tzinfo=None)
                if dt >= cutoff:
                    items.append({"source": name, "title": title, "link": link})
            except Exception:
                # If date parse fails, include it anyway (better safe)
                items.append({"source": name, "title": title, "link": link})
    except Exception as e:
        print(f"  [skip] {name}: {e}", flush=True)
    return items


def google_news_search(query):
    url = (
        "https://news.google.com/rss/search"
        f"?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    return fetch_rss("Google News", url)


def is_flagged(headline):
    h = headline.lower()
    return any(fw in h for fw in FLAG_WORDS)


def extract_events():
    """Return list of dicts: title, date_text, network."""
    with open(INDEX, encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    events = []
    for item in soup.find_all("div", class_="item"):
        date_div    = item.find("div", class_="date")
        title_div   = item.find("div", class_="title")
        network_div = item.find("div", class_="network")
        if not (date_div and title_div):
            continue

        # Remove pill badges before reading title text
        for pill in title_div.find_all(True, class_=re.compile(r"pill|badge")):
            pill.decompose()

        # First line only — skip the sub-title span
        raw = title_div.get_text(separator="\n").strip()
        title = raw.split("\n")[0].strip().lstrip("🏆 ").strip()

        if len(title) < 4:
            continue

        events.append({
            "title":   title,
            "date":    date_div.get_text().strip(),
            "network": network_div.get_text(" ", strip=True) if network_div else "",
        })
    return events


def match_event_in_headline(event_title, headline):
    """True if the headline contains enough of the event title to be a real match."""
    words = event_title.lower().split()
    # Require at least the first 3 meaningful words (skip short words for 1-2 word titles)
    key_words = [w for w in words if len(w) > 2][:4]
    if not key_words:
        return False
    phrase = " ".join(key_words[:3])
    return phrase in headline.lower()


# ---------------------------------------------------------------------------
# Main audit
# ---------------------------------------------------------------------------

def run_audit():
    today    = date.today()
    log_path = f"{LOGS_DIR}/weekly_audit_{today}.log"

    events = extract_events()
    print(f"Loaded {len(events)} events from calendar.", flush=True)

    flagged = {}  # event_title -> {"event": ..., "hits": [...]}

    # ── Step 1: scan all curated RSS feeds once ──────────────────────────
    print(f"\nScanning {len(RSS_FEEDS)} RSS feeds...", flush=True)
    all_items = []
    for name, url in RSS_FEEDS:
        print(f"  {name} ...", flush=True)
        all_items.extend(fetch_rss(name, url))
        time.sleep(0.4)

    print(f"  {len(all_items)} recent headlines collected.", flush=True)

    for item in all_items:
        if not is_flagged(item["title"]):
            continue
        for event in events:
            if match_event_in_headline(event["title"], item["title"]):
                key = event["title"]
                flagged.setdefault(key, {"event": event, "hits": []})
                existing = {h["link"] for h in flagged[key]["hits"]}
                if item["link"] not in existing:
                    flagged[key]["hits"].append(item)

    # ── Step 2: targeted Google News for events in the next 60 days ──────
    upcoming = [
        e for e in events
        if (lambda d: d is not None and today <= d <= today + timedelta(days=UPCOMING_DAYS))(
            parse_start_date(e["date"])
        )
    ]
    print(f"\nTargeted Google News search for {len(upcoming)} upcoming events...", flush=True)

    for event in upcoming:
        q = (
            f'"{event["title"]}" 2026 '
            f'(cancelled OR postponed OR rescheduled OR "date change" OR delayed OR axed)'
        )
        hits = google_news_search(q)
        for h in hits:
            if is_flagged(h["title"]):
                key = event["title"]
                flagged.setdefault(key, {"event": event, "hits": []})
                existing = {x["link"] for x in flagged[key]["hits"]}
                if h["link"] not in existing:
                    flagged[key]["hits"].append(h)
        time.sleep(0.6)

    # ── Step 3: write report ─────────────────────────────────────────────
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("fubo Calendar — Weekly Audit Report\n")
        f.write(f"Date        : {today}\n")
        f.write(f"Events      : {len(events)}\n")
        f.write(f"RSS feeds   : {len(RSS_FEEDS)}\n")
        f.write(f"Headlines   : {len(all_items)}\n")
        f.write("=" * 60 + "\n\n")

        if not flagged:
            f.write("✅  All clear — no issues found. Calendar looks good.\n")
        else:
            f.write(f"⚠️  {len(flagged)} event(s) flagged for review:\n\n")
            for title, data in sorted(flagged.items(),
                                      key=lambda x: x[1]["event"]["date"]):
                ev = data["event"]
                f.write(f"▸ {title}\n")
                f.write(f"  Date: {ev['date']}  |  Network: {ev['network']}\n")
                for hit in data["hits"][:5]:
                    f.write(f"  [{hit['source']}] {hit['title']}\n")
                    f.write(f"  → {hit['link']}\n")
                f.write("\n")

        f.write(f"\nCompleted: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    return log_path, len(flagged)


if __name__ == "__main__":
    log_path, n = run_audit()

    if n:
        msg = f"{n} event(s) may need attention — open audit log to review"
    else:
        msg = "Weekly audit complete — calendar is all clear ✅"

    subprocess.run(["osascript", "-e",
        f'display notification "{msg}" with title "fubo Calendar Audit" sound name "Glass"'])

    print(f"\n[{date.today()}] {msg}", flush=True)
    print(f"Log saved: {log_path}", flush=True)
