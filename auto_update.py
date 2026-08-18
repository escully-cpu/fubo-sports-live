"""
auto_update.py
Discovers and adds new events to the calendar automatically.
Runs every day at 9:00 AM via LaunchAgent (before the 9:05 AM GitHub push).

Sources:
  - TVMaze API (free, no key)         → TV premieres, finales, specials
  - TheSportsDB (free, no key)        → Major sports events
  - Network press room RSS feeds      → CBS, ESPN, Deadline, Variety
  - Google News RSS (targeted)        → FOX/ABC/CBS premiere announcements,
                                        confirmed sports event dates

Optional upgrade (still free):
  Add GEMINI_API_KEY to .env for smarter filtering via Google Gemini 1.5 Flash.
  Get a free key at: aistudio.google.com (free tier: 15 req/min, 1M tokens/day)
  Without it the script uses built-in rule-based filtering — works well.
"""

import os, re, sys, json, time, subprocess
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

sys.path.insert(0, "/Users/emmascully/Library/Python/3.9/lib/python/site-packages")
import requests

INDEX        = "/Users/emmascully/projects/fubo-sports-live/index.html"
LOGS         = "/Users/emmascully/projects/fubo-sports-live/logs"
ENV          = "/Users/emmascully/projects/fubo-sports-live/.env"
LOOKAHEAD    = 90   # days ahead to scan

# ── Network maps ─────────────────────────────────────────────────────────────

# TVMaze network name → (CSS class, column, tier)
# tier: "major" networks get a lower rating threshold; "standard" need higher ratings
ENT_NETWORKS = {
    "CBS":               ("cbs-e",       "ent", "major"),
    "ABC":               ("abc-e",       "ent", "major"),
    "Fox":               ("fox-e",       "ent", "major"),
    "FX":                ("fx-e",        "ent", "major"),
    "FXX":               ("fxx-e",       "ent", "major"),
    "Freeform":          ("freeform-e",  "ent", "standard"),
    "Hallmark Channel":  ("hallmark-e",  "ent", "standard"),
    "BET":               ("bet-e",       "ent", "standard"),
    "MTV":               ("mtv-e",       "ent", "standard"),
    "Starz":             ("starz-e",     "ent", "major"),
    "Paramount Network": ("paramount-e", "ent", "standard"),
    "CMT":               ("cmt-e",       "ent", "standard"),
    "Telemundo":         ("telemundo-e", "ent", "major"),
    "Universo":          ("telemundo-e", "ent", "standard"),
    "NBC":               ("nbc-e",       "ent", "major"),
    "Bravo":             ("paramount-e", "ent", "standard"),
    "Disney Channel":    ("disney-e",    "ent", "major"),
    "Disney Junior":     ("disney-e",    "ent", "standard"),
    "Disney XD":         ("disney-e",    "ent", "standard"),
}

MONTH_LABELS = {
    1: "January", 2: "February",  3: "March",    4: "April",
    5: "May",     6: "June",      7: "July",      8: "August",
    9: "September",10: "October",11: "November",12: "December",
}

ABBR_MONTHS = ["Jan","Feb","Mar","Apr","May","Jun",
               "Jul","Aug","Sep","Oct","Nov","Dec"]

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_env():
    env = {}
    try:
        with open(ENV) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except Exception:
        pass
    return env

def get_existing_titles(soup):
    titles = set()
    for item in soup.find_all("div", class_="item"):
        td = item.find("div", class_="title")
        if not td:
            continue
        clone = BeautifulSoup(str(td), "html.parser")
        for el in clone.find_all(True, class_=re.compile(r"pill|sub|badge")):
            el.decompose()
        t = clone.get_text().strip().lower()
        if t:
            titles.add(t[:60])
    return titles

_STOP_WORDS = {"the", "a", "an", "of", "and", "or", "in", "on", "at",
               "for", "to", "with", "awards", "award", "show", "season"}

def _key_words(title):
    """Extract significant lowercase words (4+ chars, not stop words)."""
    words = re.findall(r"[a-z]+", title.lower())
    return [w for w in words if len(w) >= 4 and w not in _STOP_WORDS]

def _stem(w):
    """First 4 chars — rough stem for overlap detection (catches plural/suffix variants)."""
    return w[:4]

def already_in_calendar(title, existing):
    t = title.lower().strip()
    # Exact prefix match
    if t[:60] in existing:
        return True
    # Sliding 20-char slug match
    if any(t[:20] in e for e in existing):
        return True
    # Keyword / stem overlap against each existing title's words
    kw = _key_words(title)
    if not kw:
        return False
    kw_stems = [_stem(w) for w in kw]
    for e in existing:
        e_kw    = _key_words(e)
        e_stems = [_stem(w) for w in e_kw]
        # Count how many candidate keywords (or their 5-char stems) hit the existing entry
        hits = sum(1 for w, s in zip(kw, kw_stems)
                   if w in e or s in e_stems)
        # One match is enough for short titles; two for longer ones
        needed = 1 if len(kw) <= 2 else 2
        if hits >= needed:
            return True
    return False

def fmt_date(d):
    return f"{ABBR_MONTHS[d.month - 1]} {d.day}"

# ── Date verification ─────────────────────────────────────────────────────────

_SINGLE_DATE_RE = re.compile(
    r'^([A-Za-z]{3})\s+(\d{1,2})$'
)

def parse_single_date(text, year=2026):
    """Parse 'Mon DD' format only — rejects ranges, approximates, weekly etc."""
    text = text.strip()
    if any(c in text for c in ['~', '–', '-', '+', '/']):
        return None
    if re.search(r'weekly|ongoing|tbd|season|thru|finals|week|month',
                 text, re.I):
        return None
    m = _SINGLE_DATE_RE.match(text)
    if not m:
        return None
    mo = MONTHS.get(m.group(1).lower())
    if not mo:
        return None
    try:
        return date(year, mo, int(m.group(2)))
    except ValueError:
        return None

def parse_end_date(date_text, year=2026):
    """Return the LAST date an event could still be active.
    Handles single dates, date ranges, month-only ranges, and '&' multi-leg
    events. Returns None for vague/recurring/TBD/ongoing dates."""
    text = (date_text or "").strip().replace("~", "").strip()
    if not text:
        return None
    low = text.lower()
    # Vague / open-ended descriptors — never prune
    if any(w in low for w in ("weekly", "ongoing", "tbd", "season",
                              "thru", "finals week", "+ ")):
        return None
    if low.endswith("+"):
        return None

    mo = {"jan":1, "feb":2, "mar":3, "apr":4, "may":5, "jun":6,
          "jul":7, "aug":8, "sep":9, "oct":10, "nov":11, "dec":12}

    # Month-only range: "May–Oct" → last day of end month
    m = re.match(r"^([A-Za-z]{3,})\s*[–\-]\s*([A-Za-z]{3,})\s*$", text)
    if m:
        end_mo = mo.get(m.group(2)[:3].lower())
        if end_mo:
            return (date(year, 12, 31) if end_mo == 12
                    else date(year, end_mo + 1, 1) - timedelta(days=1))

    # Date range: "Sep 30–Oct 2", "Oct 7–18", "Jun 3–19"
    m = re.match(r"^([A-Za-z]+)\s+\d+\s*[–\-]\s*(?:([A-Za-z]+)\s+)?(\d+)",
                 text)
    if m:
        start_mo_str = m.group(1)
        end_mo_str   = m.group(2) or start_mo_str
        end_mo = mo.get(end_mo_str[:3].lower())
        if end_mo:
            try:
                return date(year, end_mo, int(m.group(3)))
            except ValueError:
                pass

    # "May 21 & 24" → second day
    m = re.match(r"^([A-Za-z]+)\s+\d+\s*&\s*(\d+)", text)
    if m:
        mo_n = mo.get(m.group(1)[:3].lower())
        if mo_n:
            try:
                return date(year, mo_n, int(m.group(2)))
            except ValueError:
                pass

    # Single date: "May 24", "May 24 (TBD)"
    m = re.match(r"^([A-Za-z]+)\s+(\d+)", text)
    if m:
        mo_n = mo.get(m.group(1)[:3].lower())
        if mo_n:
            try:
                return date(year, mo_n, int(m.group(2)))
            except ValueError:
                pass

    # Month only — too vague to prune safely
    return None

_RUNS_THROUGH_RE = re.compile(
    r"(?:runs?\s+through|thru|airs?\s+through)\s+"
    r"([A-Za-z]{3,})\s+(\d+)", re.IGNORECASE)

def _series_end_date(item, fallback_end, year=2026):
    """If this item looks like a running TV series, try to find its true
    end date (e.g. "Runs through Jul 3"). Returns the later of the parsed
    end and the fallback. Returns None if we should skip pruning entirely
    (recurring schedule with no obvious end)."""
    title_el = item.find("div", class_="title")
    title    = (title_el.get_text() if title_el else "").lower()
    sub_el   = title_el.find("span", class_="sub") if title_el else None
    sub_text = (sub_el.get_text() if sub_el else "")
    full     = f"{title} {sub_text}"

    # Recurring weekly-show signals — don't prune by single date
    data_time = (item.get("data-time") or "").lower()
    if "every " in data_time:
        return None
    if re.search(r"\bs\d+\b", title):       # "S5", "S22"
        return None
    if re.search(r"\breturn(s|ed|ing)?\b|\bback\b|\bcontinues?\b",
                 full.lower()):
        return None                          # "returns", "back on", "continues"
    if any(kw in full.lower() for kw in
           ("season", "weekly", "premiere", "final season",
            "series finale", "episode", " series", "competition",
            "reality series", "ongoing")):
        # Try to find an explicit end date in the sub
        m = _RUNS_THROUGH_RE.search(sub_text)
        if m:
            mo_map = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                      "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
            mo = mo_map.get(m.group(1)[:3].lower())
            if mo:
                try:
                    return date(year, mo, int(m.group(2)))
                except ValueError:
                    pass
        return None  # series-ish, no parseable end → never prune by date alone
    return fallback_end

def _item_year(item, default=2026):
    """Return the year from the item's enclosing .month-block month-label,
    e.g. 'January 2027' -> 2027. Falls back to `default` if not found."""
    mb = item.find_parent("div", class_="month-block")
    if not mb:
        return default
    label = mb.find("div", class_="month-label")
    if not label:
        return default
    yr_span = label.find("span")
    if yr_span:
        try:
            return int(yr_span.get_text().strip())
        except ValueError:
            pass
    m = re.search(r"(20\d{2})", label.get_text())
    return int(m.group(1)) if m else default

def prune_past_events(soup, today, cutoff_days=5):
    """Remove events whose end date was more than `cutoff_days` ago.
    For TV series with weekly cadence (data-time 'Every ...' or title 'S5'
    or sub containing 'season'/'weekly'/'episode'/'premiere'), uses the
    explicit 'Runs through X' end date if present, otherwise skips the
    item entirely so we don't drop a still-airing series.

    Also removes month blocks that become empty as a result.
    Returns list of (title, end_date) tuples for the log."""
    pruned = []
    cutoff = today - timedelta(days=cutoff_days)
    print(f"  Pruning events ended on or before {cutoff} "
          f"(>{cutoff_days} days ago)...", flush=True)

    for item in list(soup.find_all("div", class_="item")):
        date_el  = item.find("div", class_="date")
        title_el = item.find("div", class_="title")
        if not (date_el and title_el):
            continue
        year = _item_year(item)
        end_d = parse_end_date(date_el.get_text().strip(), year=year)
        if not end_d:
            continue
        # Series-aware end check (may upgrade to "Runs through Jul 3" or skip)
        end_d = _series_end_date(item, end_d, year=year)
        if not end_d or end_d >= cutoff:
            continue
        title = _clean_title(title_el)
        print(f"    🗑  {title} ({end_d})", flush=True)
        item.decompose()
        pruned.append((title, end_d))

    # Clean up empty month blocks
    for mb in list(soup.find_all("div", class_="month-block")):
        lst = mb.find("div", class_="list")
        if lst and not lst.find("div", class_="item"):
            mb.decompose()

    print(f"    → {len(pruned)} event(s) pruned", flush=True)
    return pruned

def _clean_title(title_el):
    """Strip pill/sub/badge children and return plain title text."""
    clone = BeautifulSoup(str(title_el), "html.parser")
    for el in clone.find_all(True, class_=re.compile(r"pill|sub|badge|espn|new|latino")):
        el.decompose()
    raw = clone.get_text(separator=" ").strip()
    raw = re.sub(r'[🏆★🎬]', '', raw)
    return raw.split('\n')[0].strip()

def sportsdb_event_date(title):
    """Look up a specific event in TheSportsDB and return its confirmed date."""
    try:
        url = (f"https://www.thesportsdb.com/api/v1/json/3/searchevents.php"
               f"?e={requests.utils.quote(title)}&s=2026")
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        events = r.json().get("event") or []
        kw = _key_words(title)
        kw_stems = [_stem(w) for w in kw]
        for ev in events:
            ev_name = ev.get("strEvent", "").lower()
            ev_kw   = _key_words(ev_name)
            ev_stems = [_stem(w) for w in ev_kw]
            hits = sum(1 for w, s in zip(kw, kw_stems)
                       if w in ev_name or s in ev_stems)
            needed = 1 if len(kw) <= 2 else 2
            if hits >= needed:
                date_str = ev.get("dateEvent", "")
                if date_str:
                    return datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        pass
    return None

def _utc_time_to_et(time_str, ev_date):
    """Convert a UTC 'HH:MM:SS' time on a given date into a formatted ET string."""
    if not time_str or not ZoneInfo or len(time_str) < 5:
        return None
    try:
        parts = time_str.split(":")
        h, m = int(parts[0]), int(parts[1])
        dt_utc = datetime(ev_date.year, ev_date.month, ev_date.day, h, m,
                          tzinfo=ZoneInfo("UTC"))
        dt_et  = dt_utc.astimezone(ZoneInfo("America/New_York"))
        return dt_et.strftime("%-I:%M %p ET")
    except Exception:
        return None

def sportsdb_event_full(title):
    """Look up an event in TheSportsDB and return
    {date,time_et,venue,city,country,matchup}."""
    try:
        url = (f"https://www.thesportsdb.com/api/v1/json/3/searchevents.php"
               f"?e={requests.utils.quote(title)}&s=2026")
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        events = r.json().get("event") or []
        kw       = _key_words(title)
        kw_stems = [_stem(w) for w in kw]
        for ev in events:
            ev_name  = ev.get("strEvent", "").lower()
            ev_kw    = _key_words(ev_name)
            ev_stems = [_stem(w) for w in ev_kw]
            hits = sum(1 for w, s in zip(kw, kw_stems)
                       if w in ev_name or s in ev_stems)
            needed = 1 if len(kw) <= 2 else 2
            if hits < needed:
                continue
            date_str = ev.get("dateEvent", "")
            if not date_str:
                continue
            try:
                ev_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                continue
            home = (ev.get("strHomeTeam") or "").strip()
            away = (ev.get("strAwayTeam") or "").strip()
            matchup = f"{away} @ {home}" if home and away else ""
            return {
                "date":    ev_date,
                "time_et": _utc_time_to_et(ev.get("strTime", ""), ev_date),
                "venue":   (ev.get("strVenue") or "").strip(),
                "city":    (ev.get("strCity") or "").strip(),
                "country": (ev.get("strCountry") or "").strip(),
                "matchup": matchup,
            }
    except Exception:
        pass
    return None

def tvmaze_premiere_date(show_name):
    """Return the next confirmed air date for a show from TVMaze."""
    try:
        url = (f"https://api.tvmaze.com/singlesearch/shows"
               f"?q={requests.utils.quote(show_name)}&embed=nextepisode")
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        data = r.json()
        ep = (data.get("_embedded") or {}).get("nextepisode") or {}
        airdate = ep.get("airdate", "")
        if airdate:
            return datetime.strptime(airdate, "%Y-%m-%d").date()
    except Exception:
        pass
    return None

ENT_CSS = set(ENT_NETWORKS[n][0] for n in ENT_NETWORKS)

def verify_existing_dates(soup):
    """
    Cross-check specific future calendar dates against TheSportsDB and TVMaze.
    Auto-corrects mismatches in-place. Returns list of (title, old, new) tuples.
    """
    today     = date.today()
    corrections = []

    print("  Verifying existing event dates...", flush=True)

    for item in soup.find_all("div", class_="item"):
        date_el  = item.find("div", class_="date")
        title_el = item.find("div", class_="title")
        if not (date_el and title_el):
            continue

        raw_date = date_el.get_text().strip()
        d = parse_single_date(raw_date)
        if not d or d <= today:
            continue

        title      = _clean_title(title_el)
        if len(title) < 4:
            continue

        item_classes = item.get("class", [])
        verified     = None

        if "wwe" in item_classes:
            verified = sportsdb_event_date(title)
            time.sleep(0.3)
        elif "soccer" in item_classes:
            verified = sportsdb_event_date(title)
            time.sleep(0.3)
        elif any(c in item_classes for c in ENT_CSS):
            verified = tvmaze_premiere_date(title)
            time.sleep(0.2)

        if verified and verified != d:
            old_str = fmt_date(d)
            new_str = fmt_date(verified)
            date_el.string = new_str
            corrections.append((title, old_str, new_str))
            print(f"    ✎ {title}: {old_str} → {new_str}", flush=True)

    print(f"    → {len(corrections)} date correction(s)", flush=True)
    return corrections

def lookup_fight_card(event_title):
    """Search Google News RSS for an announced match/fight card.
    Returns a " · "-joined string of the top matches, or None if none
    were extractable from the recent news."""
    try:
        q = quote_plus(f'"{event_title}" match card 2026')
        url = (f"https://news.google.com/rss/search?q={q}"
               f"&hl=en-US&gl=US&ceid=US:en")
        items = _fetch_rss(url)
    except Exception:
        return None
    if not items:
        return None
    # Look at the most recent ~5 headlines+blurbs and pull out "X vs Y" pairs
    vs_pattern = re.compile(
        r'([A-Z][\w\.\'\-]+(?:\s+[A-Z][\w\.\'\-]+){0,3})'
        r'\s+vs\.?\s+'
        r'([A-Z][\w\.\'\-]+(?:\s+[A-Z][\w\.\'\-]+){0,3})'
    )
    matches = []
    for it in items[:5]:
        text = f"{it.get('title','')} {it.get('description','')}"
        for m in vs_pattern.finditer(text):
            pair = f"{m.group(1).strip()} vs. {m.group(2).strip()}"
            if pair not in matches and len(matches) < 4:
                matches.append(pair)
    return " · ".join(matches) if matches else None

def tvmaze_show_full(show_name):
    """Return TVMaze airtime info for a show. For shows with a weekly
    schedule, formats as 'Every Wednesday 9:00 PM ET' so running series
    display their cadence in the expand panel."""
    try:
        url = (f"https://api.tvmaze.com/singlesearch/shows"
               f"?q={requests.utils.quote(show_name)}&embed=nextepisode")
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        data    = r.json()
        sched   = data.get("schedule") or {}
        days    = sched.get("days") or []
        ep      = (data.get("_embedded") or {}).get("nextepisode") or {}
        airtime = sched.get("time") or ep.get("airtime") or ""
        if not airtime:
            return None
        try:
            t = datetime.strptime(airtime, "%H:%M")
            time_str = t.strftime("%-I:%M %p ET")
        except Exception:
            time_str = airtime
        # If the show airs on specific weekday(s), present as a recurring slot
        if days and len(days) <= 2:
            day_str = " & ".join(days)
            return {"time_et": f"Every {day_str} {time_str}"}
        return {"time_et": time_str}
    except Exception:
        return None

# Streaming services NOT included in the Fubo base plan.
# When an item mentions one of these without a clear Fubo-accessible alternative,
# flag it for manual confirmation of when (or whether) Fubo viewers can watch.
NOT_ON_FUBO_STREAMING = [
    "hulu", "netflix", "peacock", "apple tv+", "apple tv plus",
    "disney+", "disney plus", "amazon prime", "prime video",
    "max ", "hbo max", "paramount+",
]
# Linear networks Fubo carries — if one of these is the item's network and
# the sub mentions a streaming service, the show probably airs there too
# but the linear schedule should be verified.
FUBO_LINEAR_NETWORKS = [
    "fx", "fxx", "abc", "cbs", "fox", "freeform", "bet", "mtv",
    "vh1", "hallmark", "cmt", "paramount network", "espn", "fs1",
    "fs2", "btn", "tudn", "univision", "starz",
    # Added 2026-08-18 after this list's incompleteness caused a false
    # "not available on Fubo" note on an item whose network field clearly
    # listed NBC — cross-checked against every distinct token actually
    # used in index.html's .network divs (see CLAUDE.md).
    "nbc", "telemundo", "universo", "disney channel", "tnt", "secn",
    "nfl net", "fsn", "dazn", "bein sport", "tennis ch",
]

def check_fubo_availability(item):
    """
    Only flag TRULY unavailable items — the network field must have NO Fubo
    linear channel at all. When a Fubo network IS present, viewers can watch
    via that (any additional streaming mention is a nice-to-have, not a gap
    worth noting).
    """
    if item.get("data-fubo-note"):
        return None
    sub_el = item.find("span", class_="sub")
    net_el = item.find("div", class_="network")
    if not sub_el:
        return None
    sub_text = sub_el.get_text().lower()
    net_text = (net_el.get_text() if net_el else "").lower()

    # If a Fubo linear network is in the network field, there's no gap.
    if any(n in net_text for n in FUBO_LINEAR_NETWORKS):
        return None

    streaming_hit = next((s for s in NOT_ON_FUBO_STREAMING if s in sub_text), None)
    if not streaming_hit:
        return None
    return f"Streaming-only on {streaming_hit.strip().title()} — not available on Fubo"

def verify_existing_times(soup, today):
    """
    Re-check stored data-time values against the latest SportsDB / TVMaze
    data for upcoming events (next 14 days). Updates the attribute if the
    source now returns a different time. Skips manually-curated times that
    contain extra context (parentheticals, multiple slots, em-dashes).

    Returns list of (title, old_time, new_time) tuples.
    """
    updates = []
    print("  Verifying stored times against latest sources...", flush=True)
    sports_classes = {"soccer", "wwe", "nfl", "nba", "nhl", "mlb",
                      "wnba", "college", "college-bb", "tennis", "golf", "racing"}
    ent_classes    = {"cbs-e", "abc-e", "nbc-e", "fox-e", "fx-e", "fxx-e",
                      "freeform-e", "hallmark-e", "bet-e", "mtv-e", "starz-e",
                      "paramount-e", "cmt-e", "telemundo-e", "disney-e"}

    for item in soup.find_all("div", class_="item"):
        old_time = item.get("data-time")
        if not old_time:
            continue
        # Skip manually-curated times (parenthetical context, multi-slot, etc.)
        if any(c in old_time for c in ("(", "·", "—", "&")):
            continue
        date_el  = item.find("div", class_="date")
        title_el = item.find("div", class_="title")
        if not (date_el and title_el):
            continue
        d = parse_single_date(date_el.get_text().strip())
        if not d or d <= today or (d - today).days > 14:
            continue

        classes = set(item.get("class", []))
        title = _clean_title(title_el)
        if len(title) < 4:
            continue

        details = None
        if classes & sports_classes:
            details = sportsdb_event_full(title)
            time.sleep(0.3)
        elif classes & ent_classes:
            details = tvmaze_show_full(title)
            time.sleep(0.2)
        if not details:
            continue

        new_time = details.get("time_et")
        if new_time and new_time != old_time:
            item["data-time"] = new_time
            updates.append((title, old_time, new_time))
            print(f"    ⏰ {title}: {old_time} → {new_time}", flush=True)

    print(f"    → {len(updates)} time update(s)", flush=True)
    return updates

_TBD_MONTH_RE = re.compile(
    r"^~?\s*([A-Za-z]+)(?:\s+\d+)?\s*\(?\s*TBD\s*\)?\s*$", re.IGNORECASE)

def resolve_tbd_dates(soup, today):
    """
    For events with 'Month (TBD)' style dates, try SportsDB / TVMaze to
    pull a real date. If found and still in the future, replace the date
    string in-place. Returns list of (title, old_date, new_date) tuples.
    """
    resolved = []
    print("  Resolving TBD-dated events...", flush=True)
    sports_classes = {"soccer", "wwe", "nfl", "nba", "nhl", "mlb",
                      "wnba", "college", "college-bb", "tennis", "golf", "racing"}
    ent_classes    = {"cbs-e", "abc-e", "fox-e", "fx-e", "fxx-e", "freeform-e",
                      "hallmark-e", "bet-e", "mtv-e", "starz-e", "paramount-e",
                      "cmt-e", "telemundo-e", "nbc-e", "disney-e"}

    for item in soup.find_all("div", class_="item"):
        date_el  = item.find("div", class_="date")
        title_el = item.find("div", class_="title")
        if not (date_el and title_el):
            continue
        raw_date = date_el.get_text().strip()
        if "tbd" not in raw_date.lower() and "fall" not in raw_date.lower():
            continue
        classes = set(item.get("class", []))
        title = _clean_title(title_el)
        if len(title) < 4:
            continue

        details = None
        if classes & sports_classes:
            details = sportsdb_event_full(title)
            time.sleep(0.3)
        elif classes & ent_classes:
            # TVMaze premiere_date returns just the date
            new_d = tvmaze_premiere_date(title)
            time.sleep(0.2)
            if new_d:
                details = {"date": new_d}

        if not details or not details.get("date"):
            continue
        new_d = details["date"]
        if new_d < today:
            continue
        new_str = fmt_date(new_d)
        date_el.string = new_str
        resolved.append((title, raw_date, new_str))
        print(f"    📅 {title}: {raw_date} → {new_str}", flush=True)

    print(f"    → {len(resolved)} TBD date(s) resolved", flush=True)
    return resolved

def enrich_event_details(soup, today):
    """
    For each upcoming item that doesn't yet have a stored time/venue,
    look it up in TheSportsDB (sports) or TVMaze (entertainment) and set
    data-time, data-venue, data-city, data-country attributes on the item.
    Also runs a Fubo-availability check (streaming-service mentions) and
    sets data-fubo-note where applicable.

    Stores nothing on items that already have details, so reruns are cheap.
    Returns count of items enriched this run.
    """
    enriched = 0
    print("  Enriching event details (time/venue/location)...", flush=True)
    sports_classes = {"soccer", "wwe", "nfl", "nba", "nhl", "mlb",
                      "wnba", "college", "college-bb", "tennis", "golf", "racing"}
    ent_classes    = {"cbs-e", "abc-e", "nbc-e", "fox-e", "fx-e", "fxx-e",
                      "freeform-e", "hallmark-e", "bet-e", "mtv-e", "starz-e",
                      "paramount-e", "cmt-e", "telemundo-e", "disney-e"}

    for item in soup.find_all("div", class_="item"):
        date_el  = item.find("div", class_="date")
        title_el = item.find("div", class_="title")
        if not (date_el and title_el):
            continue
        d = parse_single_date(date_el.get_text().strip())
        if not d or d <= today:
            continue
        classes = set(item.get("class", []))
        title = _clean_title(title_el)
        if len(title) < 4:
            continue

        # Fubo availability check runs every day (cheap, no API call)
        fubo_note = check_fubo_availability(item)
        if fubo_note:
            item["data-fubo-note"] = fubo_note
            enriched += 1
            print(f"    ⓘ {title}: Fubo → {fubo_note[:60]}", flush=True)

        # Matchup check for championship-style events: keep retrying daily
        # until teams are determined (playoffs conclude → finals get teams).
        championship_kws = ("finals", "championship", "stanley cup", "world series",
                            "super bowl", "world cup final", "title game",
                            "champions league final")
        if (not item.get("data-matchup")
                and any(kw in title.lower() for kw in championship_kws)
                and classes & sports_classes):
            mu_details = sportsdb_event_full(title)
            time.sleep(0.3)
            if mu_details and mu_details.get("matchup"):
                item["data-matchup"] = mu_details["matchup"]
                enriched += 1
                print(f"    🆚 {title}: matchup → {mu_details['matchup']}", flush=True)

        # Card check for fight events (WWE PLEs, UFC, boxing) — daily search
        # for "<Event> match card" via Google News until populated.
        if (not item.get("data-card")
                and ("wwe" in classes or "ufc" in title.lower()
                     or "boxing" in title.lower())):
            card = lookup_fight_card(title)
            time.sleep(0.4)
            if card:
                item["data-card"] = card
                enriched += 1
                print(f"    🥊 {title}: card → {card[:60]}…", flush=True)

        # Skip time/venue lookup if already done
        if item.get("data-time"):
            continue

        # Multi-day tournaments (date field is a range) have too many sessions
        # to force a single time. Expand panel already renders a
        # "Multi-day event — see broadcaster schedule" placeholder for these.
        raw_date = date_el.get_text().strip()
        if re.search(r"[–\-]", raw_date) and not raw_date.startswith("~"):
            # Except for entries that already look like they name a specific
            # date (e.g. "Sep 30–Oct 2"), skip — the JS placeholder handles it.
            if not re.search(r"\bevery\b", raw_date, re.I):
                continue

        details = None
        if classes & sports_classes:
            details = sportsdb_event_full(title)
            time.sleep(0.3)
        elif classes & ent_classes:
            details = tvmaze_show_full(title)
            time.sleep(0.2)
        if not details:
            continue

        changed = False
        if details.get("time_et") and not item.get("data-time"):
            item["data-time"] = details["time_et"]; changed = True
        if details.get("venue") and not item.get("data-venue"):
            item["data-venue"] = details["venue"]; changed = True
        if details.get("city") and not item.get("data-city"):
            item["data-city"] = details["city"]; changed = True
        if details.get("country") and not item.get("data-country"):
            item["data-country"] = details["country"]; changed = True
        if changed:
            enriched += 1
            bits = [details.get("time_et") or "—",
                    details.get("venue") or details.get("city") or "—"]
            print(f"    ✎ {title}: {' · '.join(bits)}", flush=True)

    print(f"    → {enriched} item(s) enriched", flush=True)
    return enriched

# ── TVMaze data source ────────────────────────────────────────────────────────

def tvmaze_schedule(day):
    url = f"https://api.tvmaze.com/schedule?country=US&date={day.strftime('%Y-%m-%d')}"
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []

AWARD_KEYWORDS = [
    "award", "grammy", "oscar", "emmy", "tony", "golden globe",
    "espy", "cma", "ama", "vma", "people's choice", "sag ",
    "critics choice", "screen actors", "kids' choice",
]

SKIP_GENRES = {"Talk", "News", "Game Show"}
SKIP_TITLE_WORDS = [
    "late show", "late night", "tonight show", "daily show", "colbert",
    "fallon", "kimmel", "meyers", "conan", "morning", "good morning",
    "local news", "evening news", "nightly news", "sportscenter",
]

def is_significant(ep, show, network_tier):
    """
    Returns (include: bool, reason: str, pill: str).
    Uses show rating, genre, episode type, and network tier to decide.
    """
    show_name  = (show.get("name") or "").strip()
    ep_name    = (ep.get("name") or "").lower()
    ep_num     = ep.get("number") or 0
    season     = ep.get("season") or 0
    ep_type    = (ep.get("type") or "").lower()
    genres     = [g for g in (show.get("genres") or [])]
    rating_val = (show.get("rating") or {}).get("average") or 0.0

    name_lower = show_name.lower()

    # Skip talk/news/game show formats
    if any(g in SKIP_GENRES for g in genres):
        return False, "", ""
    if any(w in name_lower for w in SKIP_TITLE_WORDS):
        return False, "", ""

    # Award shows — always include
    if any(k in name_lower for k in AWARD_KEYWORDS):
        return True, "award show", ""

    # Holiday programming on Hallmark / Freeform — include
    holiday_kw = ["christmas", "holiday", "halloween", "thanksgiving", "25 days", "31 nights"]
    if any(k in name_lower for k in holiday_kw):
        return True, "holiday programming", ""

    # Specials
    if ep_type == "significant_special" or "special" in ep_name:
        if rating_val >= 6.0 or network_tier == "major":
            return True, "special", ""

    # Series premiere (S01E01)
    if ep_num == 1 and season == 1:
        threshold = 5.5 if network_tier == "major" else 6.5
        if rating_val >= threshold or rating_val == 0.0:
            return True, "series premiere", "New Series"

    # Season premiere (SxxE01, season > 1)
    if ep_num == 1 and season > 1:
        threshold = 6.5 if network_tier == "major" else 7.8
        if rating_val >= threshold:
            return True, f"S{season:02d} premiere", ""

    # Series finale — any episode whose name signals it
    finale_kw = ["series finale", "final episode", "series wrap", "last episode",
                 "series ender", "show finale"]
    if any(k in ep_name for k in finale_kw):
        if rating_val >= 6.0 or network_tier == "major":
            return True, "series finale", "Series Finale"

    return False, "", ""

def discover_tv(existing, start, end):
    candidates = []
    seen_shows  = set()
    day = start
    print(f"  TVMaze: scanning {(end - start).days} days...", flush=True)

    while day <= end:
        episodes = tvmaze_schedule(day)
        for ep in episodes:
            show    = ep.get("show", {})
            network = (show.get("network") or {}).get("name", "")
            if network not in ENT_NETWORKS:
                continue

            css, col, tier = ENT_NETWORKS[network]
            include, reason, pill_label = is_significant(ep, show, tier)
            if not include:
                continue

            show_name = show.get("name", "")
            if already_in_calendar(show_name, existing):
                continue

            key = f"{show_name}|{ep.get('season')}"
            if key in seen_shows:
                continue
            seen_shows.add(key)

            candidates.append({
                "column":     col,
                "date":       day,
                "title":      show_name,
                "season":     ep.get("season"),
                "ep_num":     ep.get("number"),
                "reason":     reason,
                "pill_label": pill_label,
                "network":    network,
                "css":        css,
                "rating":     (show.get("rating") or {}).get("average") or 0.0,
                "summary":    re.sub(r"<[^>]+>", "",
                              (show.get("summary") or ""))[:100].strip(),
            })
        time.sleep(0.12)
        day += timedelta(days=1)

    print(f"    → {len(candidates)} TV candidates", flush=True)
    return candidates

# ── TheSportsDB data source ───────────────────────────────────────────────────

SIG_SPORT_KW = [
    "final", "championship", "all-star", "all star", "draft",
    "playoff", "super bowl", "world series", "stanley cup",
    "nba finals", "wnba final", "masters", "open championship",
    "world cup", "gold cup", "bowl game", "conference championship",
    "wildcard", "wild card", "divisional", "semifinal", "semi-final",
    "quarterfinal", "quarter-final", "title game", "ncaa", "march madness",
    "matchday", "round of", "group stage", "league phase", "liguilla",
]

LEAGUES = [
    ("NFL",                   "nfl"),
    ("NBA",                   "nba"),
    ("NHL",                   "nhl"),
    ("MLB",                   "mlb"),
    ("WNBA",                  "wnba"),
    ("PGA Tour",              "golf"),
    ("Tennis",                "tennis"),
    ("UEFA Champions League", "soccer"),
    ("Copa Libertadores",     "soccer"),
    ("NWSL",                  "soccer"),
    ("Liga MX",               "soccer"),
]

# ── Recurring events manifest ─────────────────────────────────────────────────
# Events that MUST exist in the calendar for their expected month(s).
# Checked every morning — any that are absent trigger a SportsDB lookup + add.
# (title_fragment_to_match, sportsdb_search_query, expected_months, css, network_html)
RECURRING_MANIFEST = [
    # UCL 2025-26 current season
    ("UCL Semifinal",           "UEFA Champions League Semifinal",     [4, 5],       "soccer",     "CBS Sports<br/>DAZN (CA)"),
    ("UCL Final",               "UEFA Champions League Final",         [5, 6],       "soccer big", "CBS<br/>DAZN (CA)"),
    # UCL 2026-27 league phase matchdays (Sep–Dec)
    ("UCL 2026-27 — Matchday 1","UEFA Champions League Matchday",      [8, 9],       "soccer",     "CBS Sports<br/>DAZN (CA)"),
    ("UCL 2026-27 — Matchday 2","UEFA Champions League Matchday",      [9, 10],      "soccer",     "CBS Sports<br/>DAZN (CA)"),
    ("UCL 2026-27 — Matchday 3","UEFA Champions League Matchday",      [10],         "soccer",     "CBS Sports<br/>DAZN (CA)"),
    ("UCL 2026-27 — Matchday 4","UEFA Champions League Matchday",      [10, 11],     "soccer",     "CBS Sports<br/>DAZN (CA)"),
    ("UCL 2026-27 — Matchday 5","UEFA Champions League Matchday",      [11],         "soccer",     "CBS Sports<br/>DAZN (CA)"),
    ("UCL 2026-27 — Matchday 6","UEFA Champions League Matchday",      [11, 12],     "soccer",     "CBS Sports<br/>DAZN (CA)"),
    # NBA
    ("NBA Conference Finals",   "NBA Conference Finals",               [5],          "nba big",    "ABC<br/>ESPN"),
    ("NBA Finals",              "NBA Finals",                          [6],          "nba big",    "ABC<br/>ESPN"),
    # NHL
    ("Stanley Cup Final",       "NHL Stanley Cup Final",               [5, 6],       "nhl big",    "ABC<br/>ESPN"),
    # MLB postseason
    ("MLB Wild Card",           "MLB Wild Card",                       [9, 10],      "mlb",        "ABC/ESPN<br/>FOX/FS1"),
    ("MLB Division Series",     "MLB Division Series",                 [10],         "mlb",        "ABC/ESPN<br/>FOX/FS1"),
    ("MLB Championship Series", "MLB Championship Series",             [10],         "mlb",        "FOX<br/>FS1"),
    ("MLB World Series",        "MLB World Series",                    [10, 11],     "mlb big",    "FOX<br/>FS1"),
    # NFL
    ("NFL Wild Card",           "NFL Wild Card",                       [1],          "nfl big",    "FOX/CBS<br/>ESPN/ABC"),
    ("NFL Divisional",          "NFL Divisional",                      [1],          "nfl big",    "FOX/CBS<br/>ESPN/ABC"),
    ("Super Bowl",              "Super Bowl",                          [2],          "nfl big",    "FOX"),
    # Soccer cups
    ("Copa Libertadores Final", "Copa Libertadores Final",             [11],         "soccer big", "beIN Sports"),
    ("Liga MX Clausura Final",  "Liga MX Clausura Final",             [5],          "soccer big", "TUDN"),
    ("Liga MX Apertura Final",  "Liga MX Apertura Final",             [12],         "soccer big", "TUDN"),
    # Tennis Grand Slams
    ("French Open",             "French Open Roland Garros",           [5, 6],       "tennis",     "Tennis Ch.<br/>NBC"),
    ("Wimbledon",               "Wimbledon",                           [6, 7],       "tennis",     "ESPN"),
    ("US Open Tennis",          "US Open Tennis",                      [8, 9],       "tennis",     "ESPN"),
    # NWSL
    ("NWSL Championship",       "NWSL Championship",                   [11],         "soccer big", "CBS"),
    # NASCAR
    ("NASCAR Cup Championship", "NASCAR Cup Championship",             [11],         "racing big", "FOX"),
    # Serie A — Fubo Latino (FOX Deportes, 5/round) + Fubo Canada (FSN, all)
    ("Serie A Final Day",       "Serie A Matchday 38",                 [5],          "soccer big", "FOX Deportes<br/>FSN (CA)"),
    ("Serie A — Matchday 1",    "Serie A Matchday 1",                  [8],          "soccer",     "FOX Deportes<br/>FSN (CA)"),
    ("Coppa Italia",            "Coppa Italia Round",                  [9, 10, 12],  "soccer",     "FSN"),
    ("Supercoppa Italiana",     "Supercoppa Italiana",                 [12, 1],      "soccer big", "FSN"),
    # Big Ten Network — football championship
    ("Big Ten Championship",    "Big Ten Football Championship",       [12],         "college big","CBS<br/>BTN"),
    # FOX Deportes — Spanish-language Liga MX broadcasts (Fubo Latino)
    ("Liga MX Apertura — Kickoff","Liga MX Apertura",                  [7],          "soccer",     "TUDN<br/>FOX Deportes"),
    # UEFA Nations League 2026-27 group stage (Sep–Nov)
    ("UEFA Nations League — MD 1-2",  "UEFA Nations League Matchday",  [9],          "soccer",     "FS1 / FS2<br/>TUDN"),
    ("UEFA Nations League — MD 3-4",  "UEFA Nations League Matchday",  [10],         "soccer",     "FS1 / FS2<br/>TUDN"),
    ("UEFA Nations League — MD 5-6",  "UEFA Nations League Matchday",  [11],         "soccer big", "FS1 / FS2<br/>TUDN"),
    # CONCACAF Nations League 2026-27 group stage (Sep–Nov)
    ("CONCACAF Nations League — MD 1-2", "CONCACAF Nations League",    [9],          "soccer",     "CBS Sports<br/>TUDN"),
    ("CONCACAF Nations League — MD 3-4", "CONCACAF Nations League",    [10],         "soccer",     "CBS Sports<br/>TUDN"),
    ("CONCACAF Nations League — MD 5-6", "CONCACAF Nations League",    [11],         "soccer big", "CBS Sports<br/>TUDN"),
    # Other South American club finals
    ("Copa Sudamericana Final", "Copa Sudamericana Final",              [11],         "soccer big", "FS1<br/>FOX Deportes"),
    # Army-Navy Game — annual CBS classic
    ("Army-Navy Game",          "Army Navy Football",                   [12],         "college big", "CBS"),
    # AFC Asian Cup 2027 (Jan-Feb, Saudi Arabia)
    ("AFC Asian Cup",           "AFC Asian Cup",                        [1, 2],       "soccer big", "CBS Sports<br/>TUDN"),
    # Golf — PGA Tour + team events.
    # NOTE (2026-08-17): FedEx Cup Playoffs (St. Jude/BMW/TOUR Championship)
    # rotate NBC/CBS every other year through 2030 — verify the current
    # year's network via web search before trusting NBC/CBS here, don't
    # assume last year's assignment still holds. 2026 = CBS's year.
    ("BMW Championship",        "BMW Championship PGA",                 [8],          "golf",       "CBS"),
    ("TOUR Championship",       "PGA TOUR Championship East Lake",      [8],          "golf big",   "CBS"),
    ("Solheim Cup",             "Solheim Cup",                          [9],          "golf big",   "NBC"),
    ("Presidents Cup",          "Presidents Cup golf",                  [9],          "golf big",   "NBC"),
    ("Hero World Challenge",    "Hero World Challenge",                 [11, 12],     "golf",       "NBC"),
    ("The American Express",    "The American Express PGA",             [1],          "golf",       "CBS"),
    # Sentry + Sony Open in Hawaii were DROPPED from the 2027 PGA Tour
    # schedule (confirmed via web search 2026-08-17) — do not re-add.
    # Farmers Insurance Open's title sponsorship expired after 2026;
    # 2027 event still happens at Torrey Pines but under a new/TBD name —
    # intentionally left out of this manifest until a sponsor is confirmed,
    # so the audit doesn't resurrect the old "Farmers Insurance Open" name.
    # Tennis — team + Grand Slam
    ("Laver Cup",               "Laver Cup",                            [9],          "tennis",     "Tennis Ch."),
    ("Davis Cup Finals",        "Davis Cup Finals",                     [11],         "tennis big", "Tennis Ch."),
    ("Australian Open",         "Australian Open Tennis",               [1, 2],       "tennis big", "ESPN<br/>ESPN2"),
    # NHL specials
    ("NHL Winter Classic",      "NHL Winter Classic",                   [1],          "nhl big",    "TNT"),
    # Racing
    ("Winter X Games",          "Winter X Games Aspen",                 [1],          "racing",     "ESPN"),
    # NCAA Basketball — non-conference tentpoles
    ("Champions Classic",       "Champions Classic basketball",         [11],         "college-bb big", "ESPN"),
    ("Feast Week",              "Feast Week college basketball",        [11],         "college-bb", "ESPN"),
    ("ACC/SEC Challenge",       "ACC SEC Challenge basketball",         [12],         "college-bb big", "ESPN"),
    ("CBS Sports Classic",      "CBS Sports Classic basketball",        [12],         "college-bb", "CBS"),
    # CFP January cap-stone
    ("CFP National Championship","CFP National Championship",           [1],          "college big","ESPN<br/>ABC"),
]

def audit_recurring(existing, today):
    """
    Check RECURRING_MANIFEST: for each expected event whose month window
    includes today's month, verify it's in the calendar. For any that are
    missing, attempt a TheSportsDB lookup to get the real date, then return
    it as a candidate so it gets added automatically.
    """
    candidates = []
    current_month = today.month
    print("  Recurring events audit...", flush=True)

    for title_frag, search_q, months, css, network in RECURRING_MANIFEST:
        if current_month not in months:
            continue
        if already_in_calendar(title_frag, existing):
            continue

        print(f"    ⚠ Missing: {title_frag}", flush=True)
        ev_date = sportsdb_event_date(search_q)
        time.sleep(0.3)

        # Fall back to first day of current month if SportsDB has nothing
        if not ev_date or ev_date < today:
            ev_date = today

        net_display = network.replace("<br/>", " / ")
        candidates.append({
            "column":     "sports",
            "date":       ev_date,
            "title":      title_frag,
            "season":     None,
            "ep_num":     None,
            "reason":     "recurring event",
            "pill_label": "",
            "network":    net_display,
            "css":        css,
            "rating":     0.0,
            "summary":    "",
        })

    print(f"    → {len(candidates)} missing recurring event(s)", flush=True)
    return candidates


def discover_sports(existing, start, end):
    candidates = []
    print("  TheSportsDB: scanning major leagues...", flush=True)
    for league, css in LEAGUES:
        try:
            url = (f"https://www.thesportsdb.com/api/v1/json/3/searchevents.php"
                   f"?e={requests.utils.quote(league)}&s=2026")
            r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                continue
            events = (r.json().get("event") or [])
            for ev in events:
                name     = ev.get("strEvent", "")
                date_str = ev.get("dateEvent", "")
                if not date_str:
                    continue
                try:
                    ev_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                except Exception:
                    continue
                if not (start <= ev_date <= end):
                    continue
                if not any(k in name.lower() for k in SIG_SPORT_KW):
                    continue
                if already_in_calendar(name, existing):
                    continue
                candidates.append({
                    "column":     "sports",
                    "date":       ev_date,
                    "title":      name,
                    "season":     None,
                    "ep_num":     None,
                    "reason":     "major event",
                    "pill_label": "",
                    "network":    league,
                    "css":        css,
                    "rating":     0.0,
                    "summary":    "",
                })
            time.sleep(0.3)
        except Exception as e:
            print(f"    [skip] {league}: {e}", flush=True)

    print(f"    → {len(candidates)} sports candidates", flush=True)
    return candidates

# ── Press release / news discovery ───────────────────────────────────────────

# RSS feeds from official network press rooms + targeted Google News searches
PRESS_FEEDS = [
    {
        "name":    "CBS Press Express",
        "url":     "https://cbspressexpress.com/cbs-entertainment/feed/",
        "css":     "cbs-e", "col": "ent", "network": "CBS",
    },
    {
        "name":    "ESPN Press Room",
        "url":     "https://espnpressroom.com/us/feed/",
        "css":     None, "col": "sports", "network": "ESPN",
    },
    {
        "name":    "Deadline — Premiere Dates",
        "url":     "https://deadline.com/feed/",
        "css":     None, "col": None, "network": None,
    },
    {
        "name":    "Variety — TV News",
        "url":     "https://variety.com/v/tv/feed/",
        "css":     None, "col": None, "network": None,
    },
    {
        "name":    "Google News — FOX/ABC/CBS premieres",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%22premiere+date%22+2026+%28FOX+OR+ABC+OR+CBS+OR+FX+OR+Freeform%29"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": None, "network": None,
    },
    {
        "name":    "Google News — sports events announced",
        "url":     ("https://news.google.com/rss/search"
                    "?q=2026+sports+%22announced%22+OR+%22confirmed%22+OR+%22scheduled%22"
                    "+%28ESPN+OR+FOX+OR+CBS+OR+NBC%29+date"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": "sports", "network": None,
    },
    # ── FOX-specific feeds ────────────────────────────────────────────────────
    {
        "name":    "FOX Sports Press Pass",
        "url":     "https://www.foxsports.com/presspass/feed",
        "css":     None, "col": "sports", "network": "FOX",
    },
    {
        "name":    "Big Ten Network — News",
        "url":     "https://btn.com/feed/",
        "css":     None, "col": "sports", "network": "BTN",
    },
    {
        "name":    "Google News — FS1 / FS2 schedule",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%28%22FS1%22+OR+%22FS2%22%29+%22schedule%22+OR+%22announce%22+2026"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": "sports", "network": "FOX / FS1",
    },
    {
        "name":    "Google News — BTN / Big Ten Network",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%22Big+Ten+Network%22+OR+%22BTN%22+%22schedule%22+OR+%22announce%22+2026"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": "sports", "network": "BTN",
    },
    {
        "name":    "Google News — FOX Deportes",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%22FOX+Deportes%22+%28schedule+OR+announce+OR+broadcast%29+2026"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": "sports", "network": "FOX Deportes",
    },
    {
        "name":    "Google News — FOX college football / CFB",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%22college+football%22+%28FOX+OR+FS1+OR+BTN%29+schedule+2026"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": "sports", "network": "FOX / FS1 / BTN",
    },
    # ── Disney / ABC / ESPN family ────────────────────────────────────────────
    {
        "name":    "Disney corporate news",
        "url":     "https://thewaltdisneycompany.com/feed/",
        "css":     None, "col": None, "network": None,
    },
    {
        "name":    "Google News — ABC / Freeform / FX premieres",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%28ABC+OR+Freeform+OR+FX+OR+FXX%29+premiere+OR+%22series+finale%22+2026"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": "ent", "network": None,
    },
    {
        "name":    "Google News — ESPN / ABC sports announcements",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%28ESPN+OR+%22ESPN%2B%22+OR+%22ABC%22%29+%28schedule+OR+broadcast+OR+exclusive%29+sports+2026"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": "sports", "network": "ESPN / ABC",
    },
    # ── CBS / Paramount / Showtime ────────────────────────────────────────────
    {
        "name":    "Google News — CBS Sports / CBS Sports Net",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%22CBS+Sports%22+OR+%22CBS+Sports+Network%22+%28schedule+OR+broadcast%29+2026"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": "sports", "network": "CBS Sports",
    },
    {
        "name":    "Google News — Paramount Network / CMT premieres",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%28%22Paramount+Network%22+OR+CMT%29+premiere+2026"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": "ent", "network": None,
    },
    # ── Hallmark / Lifetime / BET / MTV / VH1 / Comedy Central ────────────────
    {
        "name":    "Google News — Hallmark / Lifetime premieres",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%28Hallmark+OR+Lifetime%29+%22premiere+date%22+2026"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": "ent", "network": None,
    },
    {
        "name":    "Google News — BET / MTV / VH1 / Comedy Central",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%28BET+OR+MTV+OR+VH1+OR+%22Comedy+Central%22%29+premiere+2026"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": "ent", "network": None,
    },
    # ── League-specific networks ──────────────────────────────────────────────
    {
        "name":    "Google News — NFL Network / RedZone",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%22NFL+Network%22+OR+%22NFL+RedZone%22+schedule+2026"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": "sports", "network": "NFL Network",
    },
    {
        "name":    "Google News — NHL Network / MLB Network",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%28%22NHL+Network%22+OR+%22MLB+Network%22%29+schedule+2026"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": "sports", "network": "NHL/MLB Net",
    },
    {
        "name":    "Google News — Tennis Channel / Golf Channel",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%28%22Tennis+Channel%22+OR+%22Golf+Channel%22%29+schedule+2026"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": "sports", "network": "Tennis/Golf Ch.",
    },
    {
        "name":    "Google News — ACC / SEC Network",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%28%22ACC+Network%22+OR+%22SEC+Network%22%29+schedule+2026"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": "sports", "network": "ACC/SEC Net",
    },
    # ── Fubo Latino — Spanish-language networks ───────────────────────────────
    {
        "name":    "Google News — TUDN / Univision",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%28TUDN+OR+Univision+OR+UniMas+OR+Galavision%29+%28schedule+OR+broadcast%29+2026"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": "sports", "network": "TUDN",
    },
    {
        "name":    "Google News — Telemundo / Universo",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%28Telemundo+OR+Universo%29+%28schedule+OR+broadcast+OR+premiere%29+2026"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": None, "network": "Telemundo",
    },
    {
        "name":    "Google News — beIN Sports / GolTV",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%28%22beIN+Sports%22+OR+GolTV+OR+%22ESPN+Deportes%22%29+schedule+2026"
                    "&hl=en-US&gl=US&ceid=US:en"),
        "css":     None, "col": "sports", "network": "beIN Sports",
    },
    # ── Fubo Canada — DAZN, FSN, beIN ─────────────────────────────────────────
    {
        "name":    "Google News — DAZN Canada / FSN",
        "url":     ("https://news.google.com/rss/search"
                    "?q=%28%22DAZN+Canada%22+OR+%22Fubo+Sports+Network%22+OR+%22FSN+Canada%22%29+2026"
                    "&hl=en-CA&gl=CA&ceid=CA:en"),
        "css":     None, "col": "sports", "network": "DAZN (CA)",
    },
]

# Words in a headline that signal a new event announcement
PRESS_ANNOUNCE_KW = [
    "premiere", "premieres", "debut", "debuts", "returning", "returns",
    "new series", "new show", "season finale", "series finale",
    "announces", "confirmed", "first look", "picks up", "renews",
    "ordered to series", "greenlit", "kicks off", "kicks-off",
    # Sports-broadcast specific
    "schedule", "broadcast", "to air", "will air", "exclusive coverage",
    "tip-off", "matchup", "doubleheader", "kickoff",
]

_MO_PAT = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?"
           r"|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
_DATE_IN_TEXT_RE = re.compile(
    rf"(?:on\s+)?(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?"
    rf"|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?,?\s+)?"
    rf"({_MO_PAT}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s*2026)?)",
    re.IGNORECASE,
)

# Network name → (css, col, display_name)
_NETWORK_HINTS = [
    # ── Entertainment (broadcast + cable) ─────────────────────────────────────
    (r"\bCBS\b(?!\s+Sports)",         "cbs-e",       "ent",    "CBS"),
    (r"\bABC\b(?!\s+News)",           "abc-e",       "ent",    "ABC"),
    (r"\bNBC\b(?!\s+(?:News|Deportes|Sports))", "nbc-e", "ent", "NBC"),
    (r"\bDisney\s+(?:Channel|Junior|Jr|XD)\b", "disney-e", "ent", "Disney Channel"),
    (r"\bBravo\b",                    "paramount-e", "ent",    "Bravo"),
    (r"\bFOX\b(?!\s+(?:Sports|News|Deportes|Soccer))", "fox-e", "ent", "FOX"),
    (r"\bFreeform\b",                 "freeform-e",  "ent",    "Freeform"),
    (r"\bFXX\b",                      "fxx-e",       "ent",    "FXX"),
    (r"\bFX\b",                       "fx-e",        "ent",    "FX"),
    (r"\bHallmark\b",                 "hallmark-e",  "ent",    "Hallmark"),
    (r"\bBET\b",                      "bet-e",       "ent",    "BET"),
    (r"\bMTV\b",                      "mtv-e",       "ent",    "MTV"),
    (r"\bVH1\b",                      "mtv-e",       "ent",    "VH1"),
    (r"\bComedy Central\b",           "paramount-e", "ent",    "Comedy Central"),
    (r"\bStarz\b",                    "starz-e",     "ent",    "Starz"),
    (r"\bParamount Network\b",        "paramount-e", "ent",    "Paramount Network"),
    (r"\bCMT\b",                      "cmt-e",       "ent",    "CMT"),
    (r"\bLifetime\b",                 "hallmark-e",  "ent",    "Lifetime"),
    (r"\bA&E\b",                      "paramount-e", "ent",    "A&E"),
    (r"\bHistory\s+Channel\b",        "paramount-e", "ent",    "History"),
    (r"\bTLC\b",                      "paramount-e", "ent",    "TLC"),
    (r"\bDiscovery\b",                "paramount-e", "ent",    "Discovery"),
    (r"\bAMC\b",                      "paramount-e", "ent",    "AMC"),
    (r"\bThe\s+CW\b|\bCW\s+Network\b","paramount-e", "ent",    "CW"),
    # ── Sports (US base plan) ─────────────────────────────────────────────────
    (r"\bESPN\b",                     "sports",      "sports", "ESPN"),
    (r"\bESPN2\b",                    "sports",      "sports", "ESPN2"),
    (r"\bESPNU\b",                    "sports",      "sports", "ESPNU"),
    (r"\bESPN\+\b|\bESPN\s+Plus\b",   "sports",      "sports", "ESPN+"),
    (r"\bFS1\b",                      "sports",      "sports", "FOX / FS1"),
    (r"\bFS2\b",                      "sports",      "sports", "FS2"),
    (r"\bBTN\b|\bBig Ten Network\b",  "sports",      "sports", "BTN"),
    (r"\bACC Network\b|\bACCN\b",     "sports",      "sports", "ACC Network"),
    (r"\bSEC Network\b|\bSECN\b",     "sports",      "sports", "SEC Network"),
    (r"\bCBS Sports Network\b|\bCBSSN\b", "sports",  "sports", "CBS Sports Net"),
    (r"\bCBS Sports\b",               "sports",      "sports", "CBS Sports"),
    (r"\bNFL Network\b",              "sports",      "sports", "NFL Network"),
    (r"\bNFL RedZone\b",              "sports",      "sports", "NFL RedZone"),
    (r"\bNHL Network\b",              "sports",      "sports", "NHL Network"),
    (r"\bMLB Network\b",              "sports",      "sports", "MLB Network"),
    (r"\bTennis Channel\b",           "sports",      "sports", "Tennis Ch."),
    (r"\bGolf Channel\b",             "sports",      "sports", "Golf Ch."),
    # ── Fubo Latino (Spanish-language) ────────────────────────────────────────
    (r"\bTUDN\b",                     "sports",      "sports", "TUDN"),
    (r"\bUnivision\b",                "sports",      "sports", "Univision"),
    (r"\bUniM[áa]s\b",                "sports",      "sports", "UniMás"),
    (r"\bGalavisi[óo]n\b",            "sports",      "sports", "Galavisión"),
    (r"\bTelemundo\b",                "sports",      "sports", "Telemundo"),
    (r"\bUniverso\b",                 "sports",      "sports", "Universo"),
    (r"\bNBC\s+Deportes\b",           "sports",      "sports", "NBC Deportes"),
    (r"\bbeIN\s+Sports?\b",           "sports",      "sports", "beIN Sports"),
    (r"\bESPN Deportes\b",            "sports",      "sports", "ESPN Deportes"),
    (r"\bFOX Deportes\b",             "sports",      "sports", "FOX Deportes"),
    (r"\bFOX Soccer Plus\b|\bFSP\b",  "sports",      "sports", "FOX Soccer Plus"),
    (r"\bGolTV\b",                    "sports",      "sports", "GolTV"),
    (r"\bTyC Sports\b",               "sports",      "sports", "TyC Sports"),
    # ── Fubo Canada ───────────────────────────────────────────────────────────
    (r"\bDAZN\s*(?:Canada)?\b",       "sports",      "sports", "DAZN (CA)"),
    (r"\bFubo Sports Network\b|\bFSN\b","sports",    "sports", "FSN"),
]


def _parse_press_date(text, year=2026):
    """Extract the first recognisable calendar date from a headline or blurb."""
    m = _DATE_IN_TEXT_RE.search(text)
    if not m:
        return None
    raw = m.group(1).strip()
    raw = re.sub(r"(\d+)(?:st|nd|rd|th)", r"\1", raw)
    raw = re.sub(r",?\s*2026", "", raw).strip()
    for fmt in ("%B %d", "%b %d", "%b. %d"):
        try:
            d = datetime.strptime(raw, fmt)
            return date(year, d.month, d.day)
        except ValueError:
            continue
    return None


def _infer_network(text):
    """Return (css, col, display_name) by scanning text for network mentions."""
    for pattern, css, col, name in _NETWORK_HINTS:
        if re.search(pattern, text, re.IGNORECASE):
            return css, col, name
    return None, None, None


def _extract_show_name(headline):
    """Pull a show/event name from a press release headline."""
    # Quoted title is most reliable
    m = re.search(r'[“"‘’]([^”"’]{3,60})[”"’]', headline)
    if m:
        return m.group(1).strip()
    # Strip leading "[Network] [verb]" and take the subject
    cleaned = re.sub(
        r"^(?:CBS|ABC|FOX|FX(?:X)?|Freeform|ESPN|Hallmark|BET|MTV|Starz|Paramount)\s+"
        r"(?:Announces?|Confirms?|Orders?|Picks?\s+Up|Renews?|Greenlights?|Sets?|Reveals?)\s+",
        "", headline, flags=re.IGNORECASE,
    ).strip()
    m = re.match(
        r"^([\w]['\w\s:!?&-]{3,50?}?)\s+"
        r"(?:Premiere|Returns?|Debuts?|Season\s+\d|Series\s+Finale|Gets?|Will\b|Has\b)",
        cleaned, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    return None


def _fetch_rss(url):
    """Fetch an RSS feed and return a list of {title, description, link} dicts."""
    try:
        r = requests.get(url, timeout=14,
                         headers={"User-Agent": "Mozilla/5.0 (Macintosh)"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        return [
            {
                "title":       (item.findtext("title") or "").strip(),
                "description": re.sub(r"<[^>]+>", " ",
                               (item.findtext("description") or "")).strip(),
                "link":        (item.findtext("link") or "").strip(),
            }
            for item in root.findall(".//item")
        ]
    except Exception as e:
        print(f"      [skip] {e}", flush=True)
        return []


def discover_press_releases(existing, start, end):
    """
    Scan network press room RSS feeds and Google News for new event/show
    announcements. Returns candidates in the same format as discover_tv().
    """
    candidates = []
    seen = set()
    print("  Press releases & news feeds:", flush=True)

    for feed in PRESS_FEEDS:
        print(f"    {feed['name']} ...", flush=True)
        items = _fetch_rss(feed["url"])

        for item in items:
            headline = item["title"]
            blurb    = item.get("description", "")
            combined = f"{headline} {blurb}"
            h_lower  = headline.lower()

            # Must look like an announcement
            if not any(kw in h_lower for kw in PRESS_ANNOUNCE_KW):
                continue

            # Need a parseable future date
            ev_date = _parse_press_date(combined)
            if not ev_date or not (start <= ev_date <= end):
                continue

            # Determine network
            css = feed.get("css")
            col = feed.get("col")
            net = feed.get("network")
            if not css:
                css, col, net = _infer_network(combined)
            if not css:
                continue

            # Extract show name
            show_name = _extract_show_name(headline)
            if not show_name or len(show_name) < 4:
                continue
            # Reject malformed names (unbalanced quotes, truncated, leading clutter)
            if show_name.count("'") % 2 or show_name.count('"') % 2:
                continue
            if show_name.lower().startswith(("emmy-", "the ", "a ", "an ")) and len(show_name) < 12:
                continue

            # Sports candidates must have a specific css class (not the generic "sports")
            valid_sports = {"nfl","nba","nhl","mlb","soccer","tennis","golf","wwe",
                            "college","wnba","racing"}
            if col == "sports" and css not in valid_sports:
                continue

            if already_in_calendar(show_name, existing):
                continue

            key = f"{show_name.lower()[:30]}|{ev_date}"
            if key in seen:
                continue
            seen.add(key)

            is_new = any(k in h_lower for k in
                         ("new series", "series premiere", "debut", "greenlit", "ordered to series"))
            candidates.append({
                "column":     col,
                "date":       ev_date,
                "title":      show_name,
                "season":     None,
                "ep_num":     None,
                "reason":     "press release",
                "pill_label": "New Series" if is_new else "",
                "network":    net or css,
                "css":        css,
                "rating":     0.0,
                "summary":    blurb[:80] if blurb else "",
            })

        time.sleep(0.5)

    print(f"    → {len(candidates)} press release candidates", flush=True)
    return candidates


# ── Gemini filtering (optional, free) ────────────────────────────────────────

GEMINI_PROMPT = """You maintain a FuboTV sports & entertainment calendar for 2026.
Review these candidate events discovered from TVMaze and TheSportsDB.
The calendar already has ~100 events — avoid duplicates and filler content.

INCLUDE: Season/series premieres of well-known shows, series finales, award shows,
major sports championships, drafts, All-Star games, playoff rounds.
EXCLUDE: Regular mid-season episodes, minor reality shows, niche sports, low-rated shows.

FuboTV carries: FOX, CBS, ABC, NBC, Telemundo, Universo, Bravo, FX, FXX, Freeform,
Hallmark, BET, MTV, Starz (add-on), Paramount Network, CMT, ESPN/ESPN+, FS1/FS2, BTN,
ACC Net, SEC Net, NFL Network, NHL Network, MLB Network, Tennis Channel.
NOT on FuboTV (Versant spinoff): USA Network, Syfy, E!, Oxygen, CNBC, MS NOW (formerly
MSNBC), Golf Channel. Also unavailable: Peacock, Amazon Prime, Netflix, Apple TV+.

CANDIDATES:
{candidates}

Return a JSON array of events worth adding. For each:
{{
  "column": "ent" or "sports",
  "month": "May",
  "html": "<div class=\\"item CSSCLASS\\">\\n<div class=\\"date\\">May 15</div>\\n<div class=\\"title\\">Title<span class=\\"sub\\">Brief description</span></div>\\n<div class=\\"network\\">Network</div>\\n</div>"
}}
CSS classes: cbs-e, abc-e, fox-e, fx-e, freeform-e, hallmark-e, bet-e, mtv-e,
starz-e, paramount-e, nfl, nba, nhl, mlb, soccer, tennis, golf, wwe, college, wnba.
Add class "big" for major events. Use <div class=\\"new-pill\\">New Series</div> or
<div class=\\"new-pill\\">Series Finale</div> where appropriate.
Output ONLY the JSON array. If nothing qualifies, output []."""

def filter_with_gemini(candidates, api_key):
    payload = [{
        "date":    c["date"].strftime("%Y-%m-%d"),
        "title":   c["title"],
        "network": c["network"],
        "reason":  c["reason"],
        "css":     c["css"],
        "column":  c["column"],
        "rating":  c["rating"],
        "summary": c["summary"],
        "season":  c["season"],
    } for c in candidates]

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-1.5-flash:generateContent?key={api_key}")
    body = {"contents": [{"parts": [{"text":
        GEMINI_PROMPT.format(candidates=json.dumps(payload, indent=2))
    }]}]}

    r = requests.post(url, json=body, timeout=30,
                      headers={"Content-Type": "application/json"})
    r.raise_for_status()
    text = (r.json()
              .get("candidates", [{}])[0]
              .get("content", {})
              .get("parts", [{}])[0]
              .get("text", ""))
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        return json.loads(m.group())
    return []

# ── Rule-based HTML formatting ────────────────────────────────────────────────

def format_rule_based(candidates):
    results = []
    for c in candidates:
        title = c["title"]
        season = c.get("season")
        if season and season > 1:
            title += f" S{season:02d}"

        pill = ""
        if c.get("pill_label"):
            pill = f'<div class="new-pill">{c["pill_label"]}</div>'

        sub = f'<span class="sub">{c["summary"]}</span>' if c.get("summary") else ""
        net = c["network"]
        css = c["css"]

        # Major events get "big" class
        is_big = c["reason"] in ("award show", "major event", "series premiere",
                                  "series finale", "holiday programming")
        css_full = f"{css} big" if is_big else css

        html = (f'<div class="item {css_full}">\n'
                f'<div class="date">{fmt_date(c["date"])}</div>\n'
                f'<div class="title">{title}{pill}{sub}</div>\n'
                f'<div class="network">{net}</div>\n</div>')
        results.append({
            "column": c["column"],
            "month":  MONTH_LABELS[c["date"].month],
            "html":   html,
        })
    return results

# ── HTML insertion ────────────────────────────────────────────────────────────

def find_month_list(soup, month_name, column):
    col_class = "col-ent" if column == "ent" else "col-sports"
    col_div = soup.find("div", class_=col_class)
    if not col_div:
        return None
    for mb in col_div.find_all("div", class_="month-block"):
        label = mb.find("div", class_="month-label")
        if label and month_name.lower() in label.get_text().lower():
            return mb.find("div", class_="list")
    return None

def insert_events(soup, items):
    added = 0
    for item in items:
        lst = find_month_list(soup, item["month"], item.get("column", "ent"))
        if not lst:
            print(f"  [warn] month block not found: {item['month']}", flush=True)
            continue
        new_node = BeautifulSoup(item["html"], "html.parser")
        lst.append(new_node)
        added += 1
    return added

# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    today    = date.today()
    start    = today                            # include today's events
    end      = today + timedelta(days=LOOKAHEAD)
    log_path = f"{LOGS}/auto_update_{today}.log"
    env      = load_env()
    gemini_key = env.get("GEMINI_API_KEY", "").strip()
    if gemini_key in ("", "YOUR_KEY_HERE"):
        gemini_key = None

    mode = "Gemini (free)" if gemini_key else "rule-based"
    print(f"[{today}] auto_update.py starting — mode: {mode}", flush=True)

    with open(INDEX, encoding="utf-8") as f:
        raw = f.read()
    soup = BeautifulSoup(raw, "html.parser")

    existing = get_existing_titles(soup)
    print(f"  Existing events: {len(existing)}", flush=True)

    pruned       = prune_past_events(soup, today, cutoff_days=5)
    tbd_resolved = resolve_tbd_dates(soup, today)
    corrections  = verify_existing_dates(soup)
    time_updates = verify_existing_times(soup, today)
    enrichments  = enrich_event_details(soup, today)

    tv_cands       = discover_tv(existing, start, end)
    sport_cands    = discover_sports(existing, start, end)
    press_cands    = discover_press_releases(existing, start, end)
    recurring_cands= audit_recurring(existing, today)
    all_cands      = tv_cands + sport_cands + press_cands + recurring_cands

    print(f"  Total candidates: {len(all_cands)} "
          f"(TV:{len(tv_cands)} Sports:{len(sport_cands)} "
          f"Press:{len(press_cands)} Recurring:{len(recurring_cands)})",
          flush=True)

    if (not all_cands and not corrections and not enrichments
            and not time_updates and not pruned and not tbd_resolved):
        msg = "auto_update: no new candidates found, all dates verified"
        with open(log_path, "w") as f:
            f.write(f"{msg}\nRun: {today}\nMode: {mode}\n")
        print(f"[{today}] {msg}", flush=True)
        return

    # Audit-only path (no new events to add) still warrants a save
    if not all_cands and (corrections or enrichments or time_updates or pruned or tbd_resolved):
        with open(INDEX, "w", encoding="utf-8") as f:
            f.write(str(soup))
        with open(log_path, "w") as f:
            f.write(f"auto_update — {today}\nMode: {mode}\n"
                    f"Date fixes  : {len(corrections)}\n"
                    f"TBD resolved: {len(tbd_resolved)}\n"
                    f"Time shifts : {len(time_updates)}\n"
                    f"Enriched    : {enrichments}\n"
                    f"Pruned      : {len(pruned)}\n")
            for title, old, new in tbd_resolved:
                f.write(f"  📅 {title}: {old} → {new}\n")
            for title, old, new in time_updates:
                f.write(f"  ⏰ {title}: {old} → {new}\n")
            for title, end_d in pruned:
                f.write(f"  🗑  {title} (ended {end_d})\n")
        msg_bits = []
        if pruned:       msg_bits.append(f"{len(pruned)} past event(s) pruned")
        if corrections:  msg_bits.append(f"{len(corrections)} date(s) corrected")
        if time_updates: msg_bits.append(f"{len(time_updates)} time shift(s)")
        if enrichments:  msg_bits.append(f"{enrichments} item(s) enriched")
        msg = "Calendar refreshed: " + " · ".join(msg_bits)
        subprocess.run(["osascript", "-e",
            f'display notification "{msg}" with title "fubo Calendar" sound name "Glass"'])
        print(f"[{today}] {msg}", flush=True)
        return

    if gemini_key:
        try:
            items_to_add = filter_with_gemini(all_cands, gemini_key)
            print(f"  Gemini filtered to {len(items_to_add)} items", flush=True)
        except Exception as e:
            print(f"  [Gemini error] {e} — falling back to rule-based", flush=True)
            items_to_add = format_rule_based(all_cands)
    else:
        items_to_add = format_rule_based(all_cands)

    print(f"  Items to add: {len(items_to_add)}", flush=True)

    if not items_to_add:
        msg = "auto_update: candidates found but none passed filters"
        with open(log_path, "w") as f:
            f.write(f"{msg}\nCandidates: {len(all_cands)}\nRun: {today}\n")
        print(f"[{today}] {msg}", flush=True)
        return

    added = insert_events(soup, items_to_add)

    if added > 0 or corrections or enrichments or time_updates or pruned or tbd_resolved:
        with open(INDEX, "w", encoding="utf-8") as f:
            f.write(str(soup))

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"auto_update Report — {today}\n")
        f.write(f"Mode        : {mode}\n")
        f.write(f"Candidates  : {len(all_cands)}\n")
        f.write(f"Added       : {added}\n")
        f.write(f"Pruned      : {len(pruned)}\n")
        f.write(f"TBD resolved: {len(tbd_resolved)}\n")
        f.write(f"Date fixes  : {len(corrections)}\n")
        f.write(f"Time shifts : {len(time_updates)}\n")
        f.write(f"Enriched    : {enrichments}\n")
        f.write(f"Recurring   : {len(recurring_cands)} missing\n")
        f.write("=" * 50 + "\n\n")
        if pruned:
            f.write("PRUNED (ended >5 days ago):\n")
            for title, end_d in pruned:
                f.write(f"  🗑  {title} (ended {end_d})\n")
            f.write("\n")
        if tbd_resolved:
            f.write("TBD DATES RESOLVED:\n")
            for title, old, new in tbd_resolved:
                f.write(f"  📅 {title}: {old} → {new}\n")
            f.write("\n")
        if corrections:
            f.write("DATE CORRECTIONS:\n")
            for title, old, new in corrections:
                f.write(f"  ✎ {title}: {old} → {new}\n")
            f.write("\n")
        if time_updates:
            f.write("TIME SHIFTS:\n")
            for title, old, new in time_updates:
                f.write(f"  ⏰ {title}: {old} → {new}\n")
            f.write("\n")
        for it in items_to_add:
            snippet = re.sub(r"<[^>]+>", " ", it.get("html", ""))[:80].strip()
            f.write(f"+ [{it.get('month')}] {snippet}\n")

    parts = []
    if added:
        parts.append(f"{added} new event(s) added")
    if pruned:
        parts.append(f"{len(pruned)} past event(s) pruned")
    if tbd_resolved:
        parts.append(f"{len(tbd_resolved)} TBD date(s) resolved")
    if corrections:
        parts.append(f"{len(corrections)} date(s) corrected")
    if time_updates:
        parts.append(f"{len(time_updates)} time shift(s)")
    if enrichments:
        parts.append(f"{enrichments} item(s) enriched")
    msg = ("Calendar updated: " + " · ".join(parts)
           if parts else "Calendar check complete — all clear")

    subprocess.run(["osascript", "-e",
        f'display notification "{msg}" with title "fubo Calendar" sound name "Glass"'])
    print(f"[{today}] {msg}", flush=True)
    print(f"Log: {log_path}", flush=True)


if __name__ == "__main__":
    run()
