"""
auto_update.py
Discovers and adds new events to the calendar automatically.
Runs every Monday at 9:00 AM via LaunchAgent (before the 9:05 AM GitHub push).

Sources:
  - TVMaze API (free, no key)    → TV premieres, finales, specials
  - TheSportsDB (free, no key)   → Major sports events

Optional upgrade (still free):
  Add GEMINI_API_KEY to .env for smarter filtering via Google Gemini 1.5 Flash.
  Get a free key at: aistudio.google.com (free tier: 15 req/min, 1M tokens/day)
  Without it the script uses built-in rule-based filtering — works well.
"""

import os, re, sys, json, time, subprocess
from datetime import date, datetime, timedelta
from bs4 import BeautifulSoup

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
    "Freeform":          ("freeform-e",  "ent", "standard"),
    "Hallmark Channel":  ("hallmark-e",  "ent", "standard"),
    "BET":               ("bet-e",       "ent", "standard"),
    "MTV":               ("mtv-e",       "ent", "standard"),
    "Starz":             ("starz-e",     "ent", "major"),
    "Paramount Network": ("paramount-e", "ent", "standard"),
    "CMT":               ("cmt-e",       "ent", "standard"),
}

MONTH_LABELS = {
    1: "January", 2: "February",  3: "March",    4: "April",
    5: "May",     6: "June",      7: "July",      8: "August",
    9: "September",10: "October",11: "November",12: "December",
}

ABBR_MONTHS = ["Jan","Feb","Mar","Apr","May","Jun",
               "Jul","Aug","Sep","Oct","Nov","Dec"]

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
        threshold = 7.0 if network_tier == "major" else 7.8
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
    "title game", "ncaa", "march madness",
]

LEAGUES = [
    ("NFL",     "nfl"),
    ("NBA",     "nba"),
    ("NHL",     "nhl"),
    ("MLB",     "mlb"),
    ("WNBA",    "wnba"),
    ("PGA Tour","golf"),
    ("MLS",     "soccer"),
    ("Tennis",  "tennis"),
]

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

# ── Gemini filtering (optional, free) ────────────────────────────────────────

GEMINI_PROMPT = """You maintain a FuboTV sports & entertainment calendar for 2026.
Review these candidate events discovered from TVMaze and TheSportsDB.
The calendar already has ~100 events — avoid duplicates and filler content.

INCLUDE: Season/series premieres of well-known shows, series finales, award shows,
major sports championships, drafts, All-Star games, playoff rounds.
EXCLUDE: Regular mid-season episodes, minor reality shows, niche sports, low-rated shows.

FuboTV carries: FOX, CBS, ABC, FX, Freeform, Hallmark, BET, MTV, Starz (add-on),
Paramount Network, CMT, ESPN/ESPN+, FS1/FS2, NFL Network, NBA TV, NHL Network, MLB Network.
NOT on FuboTV: NBC/Peacock, Amazon Prime, Netflix, Apple TV+.

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
    start    = today + timedelta(days=1)
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

    corrections = verify_existing_dates(soup)

    tv_cands    = discover_tv(existing, start, end)
    sport_cands = discover_sports(existing, start, end)
    all_cands   = tv_cands + sport_cands

    print(f"  Total candidates: {len(all_cands)}", flush=True)

    if not all_cands and not corrections:
        msg = "auto_update: no new candidates found, all dates verified"
        with open(log_path, "w") as f:
            f.write(f"{msg}\nRun: {today}\nMode: {mode}\n")
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

    if added > 0 or corrections:
        with open(INDEX, "w", encoding="utf-8") as f:
            f.write(str(soup))

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"auto_update Report — {today}\n")
        f.write(f"Mode      : {mode}\n")
        f.write(f"Candidates: {len(all_cands)}\n")
        f.write(f"Added     : {added}\n")
        f.write(f"Date fixes: {len(corrections)}\n")
        f.write("=" * 50 + "\n\n")
        if corrections:
            f.write("DATE CORRECTIONS:\n")
            for title, old, new in corrections:
                f.write(f"  ✎ {title}: {old} → {new}\n")
            f.write("\n")
        for it in items_to_add:
            snippet = re.sub(r"<[^>]+>", " ", it.get("html", ""))[:80].strip()
            f.write(f"+ [{it.get('month')}] {snippet}\n")

    parts = []
    if added:
        parts.append(f"{added} new event(s) added")
    if corrections:
        parts.append(f"{len(corrections)} date(s) corrected")
    msg = ("Calendar updated: " + " · ".join(parts)
           if parts else "Calendar check complete — all clear")

    subprocess.run(["osascript", "-e",
        f'display notification "{msg}" with title "fubo Calendar" sound name "Glass"'])
    print(f"[{today}] {msg}", flush=True)
    print(f"Log: {log_path}", flush=True)


if __name__ == "__main__":
    run()
