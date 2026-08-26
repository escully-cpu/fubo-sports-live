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

# Headlines containing any of these words near a show name trigger a flag.
# Kept intentionally broad — a missed real change (false negative) is worse
# than an extra line in the log (false positive). Expanded 2026-08-18 after
# "WWE Money in the Bank" reschedule coverage used "pushed ... to October"
# phrasing that the original narrower list didn't catch.
FLAG_WORDS = [
    "cancel", "cancell", "postpone", "delay", "reschedule", "push back",
    "pushed back", "pushed to", "pushed up", "bumped to", "bumped up",
    "shifted to", "moved from", "pulled", "axed", "hiatus", "not returning",
    "no longer", "date change", "new date", "moved to",
    "premiere date changed", "ending", "final season", "last season",
    "series finale", "production halt", "shut down", "won't return",
    "will not return", "backs off", "backing off", "not scheduled",
    "not on the schedule", "off the schedule", "no plans for", "skipping",
    "scrapped", "shelved", "won't happen", "instead of the", "dropped from",
]

# WWE Premium Live Events — a demonstrated failure category: WrestlePalooza
# was assumed to recur annually (it was a 2025-only launch special, never
# scheduled for 2026) and Money in the Bank's reschedule sat undetected for
# weeks because narrow OR-clause queries didn't surface the coverage that
# existed. Keyword matching alone can't be made fully reliable against
# unpredictable headline phrasing, so for this category we don't rely on
# FLAG_WORDS at all — we surface the freshest headlines unconditionally
# every week so a human can skim them in seconds instead of needing to
# think to go looking.
WWE_WATCH_KEYWORD = "wwe"

# Every check above assumes the calendar already has an entry for a given
# event and asks "is it still accurate?" That's a different question from
# "is an entire league/competition missing from the calendar altogether?"
# — which is what actually went wrong with MLS and IndyCar on 2026-08-18:
# both were carried by Fubo (IndyCar fully, MLS partially — national FOX/
# FS1 broadcasts + MLS Cup Final) and had zero representation anywhere in
# the file. No per-event check can catch an absence; this one specifically
# looks for it. Keyword variants per league — matched against every
# existing calendar title, case-insensitive substring. A league with ZERO
# matches gets flagged for manual research (confirm current Fubo carriage
# via WebSearch before adding — see CLAUDE.md), same as every other
# "surface for a human, don't auto-decide" check in this file.
#
# This list is deliberately broad — includes leagues that may NOT be on
# Fubo (so their absence is correct) alongside ones that should be. A
# flag here means "verify," not "definitely add." Update this list when
# a league's Fubo status is confirmed one way or the other, and note the
# reasoning so the next read of this file doesn't have to re-derive it.
LEAGUE_COVERAGE_CHECKLIST = [
    ("NFL",                      ["nfl"]),
    ("NBA",                      ["nba"]),
    ("MLB",                      ["mlb"]),
    ("NHL",                      ["nhl", "stanley cup", "winter classic"]),
    ("NCAA Football",            ["college football", "cfp", "bowl", "heisman", "army-navy", "army navy"]),
    ("NCAA Basketball",          ["ncaa basketball", "college basketball", "march madness", "final four", "champions classic", "feast week"]),
    ("WNBA",                     ["wnba"]),
    ("MLS",                      ["mls"]),
    ("NWSL",                     ["nwsl"]),
    ("Leagues Cup",              ["leagues cup"]),
    ("EPL",                      ["epl", "premier league", "manchester derby"]),
    ("La Liga",                  ["la liga"]),
    ("Serie A",                  ["serie a"]),
    ("Bundesliga",               ["bundesliga"]),
    ("UEFA Champions League",    ["champions league", "ucl "]),
    ("UEFA Nations League",      ["uefa nations league"]),
    ("CONCACAF Nations League",  ["concacaf nations league"]),
    ("Copa Libertadores",        ["copa libertadores"]),
    ("Copa Sudamericana",        ["copa sudamericana"]),
    ("AFC Asian Cup",            ["asian cup"]),
    ("NASCAR",                   ["nascar"]),
    ("IndyCar",                  ["indycar", "indy car"]),
    ("PGA Tour majors/playoffs", ["pga", "fedex cup", "bmw championship", "tour championship", "the sentry", "farmers insurance", "torrey pines", "american express"]),
    ("Ryder/Presidents/Solheim Cup", ["ryder cup", "presidents cup", "solheim cup"]),
    ("Tennis Grand Slams",       ["australian open", "french open", "wimbledon", "us open tennis"]),
    ("ATP/WTA team & Masters events", ["laver cup", "davis cup", "masters 1000", "wta finals", "atp finals", "rolex masters", "national bank open", "western & southern"]),
    ("WWE",                      ["wwe"]),
    ("Little League World Series", ["little league world series"]),
    ("Winter/Summer X Games",    ["x games"]),
]

# Leagues deliberately left OUT of the active checklist above — flagging
# these every week would be a standing false alarm, not a real gap, and
# the whole point of check_wwe_schedule_watch's design philosophy applies
# here too: a check nobody trusts because it "always finds something" is
# worse than no check. Re-add to LEAGUE_COVERAGE_CHECKLIST once each
# tournament's actual window approaches.
#   - CONCACAF Champions Cup: 2026 edition already ran and concluded
#     Feb 3 - May 30, 2026 (Toluca won on penalties) — entirely before
#     this calendar's coverage window started. Confirmed on Fubo via
#     FS1/FS2 (TUDN was also reported as carrying it, but TUDN itself
#     has been off Fubo since Dec 2024 — don't credit TUDN as a
#     Fubo-available network when re-adding). Next edition starts
#     ~Feb 2027 — re-add to the active checklist around Jan 2027.
#   - Gold Cup: biennial, odd years only (2025, 2027, ...) — no 2026
#     edition exists. Re-add ahead of the 2027 tournament.
#   - Copa América: next edition is 2028 (last was 2024) — no 2026
#     edition exists. Re-add ahead of the 2028 tournament.
#   - Liga MX: removed 2026-08-26 — its only Fubo-available broadcaster
#     was TUDN, which has been off Fubo since a Dec 2024 carriage
#     dispute with TelevisaUnivision (not yet restored, see CLAUDE.md).
#     No other Fubo-carried network currently has Liga MX rights (FOX
#     Deportes carries some Apertura/Clausura regular-season coverage —
#     that's tracked via RECURRING_MANIFEST's "Liga MX Apertura —
#     Kickoff" entry, not this checklist). Re-add once TUDN is restored
#     or another Fubo network picks up Liga MX Final rights.

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

def check_espn_deportes_coverage(events):
    """Flag major sports events that are missing ESPN Deportes coverage.
    Keyword fragments are specific enough to avoid false positives like
    'Little League World Series' matching 'World Series', or the tennis
    'Rolex Masters' events matching golf's 'Masters'."""
    major_sports = [
        "us open tennis", "the open championship", "masters tournament",
        "pga tour", "wimbledon", "french open", "australian open",
        "mlb world series", "super bowl", "stanley cup final", "nba finals",
        "champions league", "copa américa", "copa america",
    ]
    missing_coverage = []

    for event in events:
        title = event["title"].lower()
        network = event["network"].lower()

        for sport in major_sports:
            if sport in title:
                if "deportes" not in network:
                    missing_coverage.append({
                        "title": event["title"],
                        "date": event["date"],
                        "network": event["network"],
                        "sport": sport,
                    })
                break

    return missing_coverage



# Properties with a history of rotating, recently-renegotiated, or otherwise
# volatile US TV rights — worth a standing watch since getting these wrong
# from memory has actually happened (TOUR Championship/BMW Championship
# were mis-typed as NBC when 2026 is CBS's year in the FedEx Cup Playoffs
# rotation; see CLAUDE.md). Keep this list in sync with RECURRING_MANIFEST
# in auto_update.py — same properties, same reason to be suspicious.
RIGHTS_SENSITIVE_KEYWORDS = [
    "BMW Championship", "TOUR Championship", "FedEx St. Jude",
    "St. Jude Championship", "Presidents Cup", "Solheim Cup", "Ryder Cup",
    "The Sentry", "Sony Open", "American Express", "Farmers Insurance",
    "Torrey Pines", "Hero World Challenge", "Genesis Invitational",
    "WM Phoenix Open", "Pebble Beach", "Manchester Derby",
]

RIGHTS_CHANGE_WORDS = [
    "moves to", "new deal", "media rights", "network change",
    "switches network", "changes network", "new tv deal", "leaves nbc",
    "leaves cbs", "leaves espn", "leaves fox", "joins cbs", "joins nbc",
    "joins espn", "joins fox", "signs deal", "rights deal", "tv deal",
    "broadcast deal", "loses rights", "acquires rights", "wins rights",
]


def check_network_rights_changes(events, today):
    """For a curated list of historically volatile TV-rights properties,
    run a targeted Google News search and flag any headline that suggests
    a network/rights change. This is the automated half of the fix for
    the 2026-08-17 incident where TOUR Championship/BMW Championship were
    mis-typed as NBC from memory when 2026 is actually CBS's year in the
    FedEx Cup Playoffs' NBC/CBS rotation. RSS/Google News is free — no
    API key, same mechanism already used for cancellation scanning."""
    watched = [
        e for e in events
        if any(k.lower() in e["title"].lower() for k in RIGHTS_SENSITIVE_KEYWORDS)
    ]
    print(f"  Watching {len(watched)} rights-sensitive event(s)...", flush=True)

    flags = []
    year = today.year
    for event in watched:
        q = (
            f'"{event["title"]}" {year} '
            f'(network OR broadcast OR TV) '
            f'("moves to" OR "new deal" OR "media rights" OR "network change" '
            f'OR "switches" OR "signs deal" OR "rights deal")'
        )
        hits = google_news_search(q)
        for h in hits:
            headline = h["title"].lower()
            if any(w in headline for w in RIGHTS_CHANGE_WORDS):
                flags.append({
                    "title": event["title"],
                    "date": event["date"],
                    "network": event["network"],
                    "headline": h["title"],
                    "link": h["link"],
                })
        time.sleep(0.6)

    print(f"    → {len(flags)} possible rights-change signal(s)", flush=True)
    return flags


def check_wwe_schedule_watch(events, today):
    """For every WWE event in the calendar, run a loose (non-restrictive)
    Google News search and surface the freshest headlines unconditionally
    — no FLAG_WORDS filtering. See the WWE_WATCH_KEYWORD comment above for
    why: this category has already produced two real misses (WrestlePalooza
    assumed-recurring, Money in the Bank reschedule) that a keyword-gated
    search failed to catch, because the actual news coverage didn't use
    any of the words we were watching for. A human skimming 2-3 fresh
    headlines per event is more reliable than trying to enumerate every
    possible phrasing of 'this got cancelled/moved/dropped' in advance."""
    watched = [e for e in events if WWE_WATCH_KEYWORD in e["title"].lower()]
    print(f"  Watching {len(watched)} WWE event(s) for schedule drift...", flush=True)

    digest = []
    year = today.year
    for event in watched:
        q = f'"{event["title"]}" {year}'
        hits = google_news_search(q)
        # Freshest first — RSS feeds are typically already newest-first,
        # but don't rely on it silently.
        hits = hits[:3]
        if hits:
            digest.append({
                "title": event["title"],
                "date": event["date"],
                "network": event["network"],
                "headlines": hits,
            })
        time.sleep(0.6)

    print(f"    → {len(digest)} event(s) with fresh coverage to skim", flush=True)
    return digest


def check_league_coverage_gaps(events):
    """Flag any league/competition in LEAGUE_COVERAGE_CHECKLIST with zero
    matching titles anywhere in the calendar. Pure local string matching —
    no network calls, can't fail or rate-limit. This is the check that
    would have caught MLS and IndyCar being completely absent; every other
    check in this file audits accuracy of EXISTING entries, which
    structurally cannot detect a league that was never added at all."""
    all_titles = " ||| ".join(e["title"].lower() for e in events)
    missing = []
    for league, keywords in LEAGUE_COVERAGE_CHECKLIST:
        if not any(kw in all_titles for kw in keywords):
            missing.append(league)
    print(f"    → {len(missing)} league(s) with zero calendar presence", flush=True)
    return missing


# Major broadcast/cable networks worth a standing schedule-announcement
# watch. Mirrors LEAGUE_COVERAGE_CHECKLIST's role for sports, but for
# entertainment the equivalent of "is a whole league missing" doesn't
# really apply (every major network already has SOME presence) — the
# actual failure mode found 2026-08-20 was narrower and structural:
# auto_update.py's TVMaze scan can only find a show once TVMaze has
# ingested its schedule, and that ingestion visibly lags real network
# announcements by weeks (confirmed empirically: FOX's Fall 2026 fall
# schedule was fully public via press coverage in July, but TVMaze's
# schedule endpoint returned ZERO Fox episodes for the actual September
# premiere dates as late as August 20). The press-release RSS scanner
# (PRESS_FEEDS, discover_press_releases) DOES surface the right
# headlines ("Fox Fall 2026 Premiere Dates: Full Schedule...") but
# _parse_press_date() can't extract a date from them — these are
# roundup articles that list per-show dates in the body, not a single
# date near the headline, and no regex fix closes that gap without
# fetching and parsing full article bodies.
ENTERTAINMENT_NETWORKS_WATCH = [
    "CBS", "ABC", "FOX", "NBC", "FX", "Freeform", "Hallmark Channel",
    "BET", "MTV", "Paramount Network", "Telemundo",
]


def check_network_schedule_announcements(today):
    """Unconditional digest of 'fall/midseason schedule' announcement
    headlines per major network — same philosophy as
    check_wwe_schedule_watch(): don't try to auto-classify or extract
    structured data from something that's proven unparseable, just
    guarantee the relevant headlines are visible every week so a human
    can skim and manually cross-check the calendar in minutes instead of
    needing to think to go looking. Most valuable during premiere-
    announcement season (roughly May-August for fall, Dec-Jan for
    midseason) but runs every week regardless — low cost, always current."""
    year = today.year
    digest = []
    for network in ENTERTAINMENT_NETWORKS_WATCH:
        q = f'"{network}" ({year} OR {year+1}) (premiere dates OR "fall schedule" OR "midseason schedule" OR "schedule announced")'
        hits = google_news_search(q)[:2]
        if hits:
            digest.append({"network": network, "headlines": hits})
        time.sleep(0.5)
    print(f"    → {len(digest)} network(s) with fresh schedule coverage to skim", flush=True)
    return digest


def run_audit():
    today    = date.today()
    log_path = f"{LOGS_DIR}/weekly_audit_{today}.log"

    events = extract_events()
    print(f"Loaded {len(events)} events from calendar.", flush=True)

    flagged = {}  # event_title -> {"event": ..., "hits": [...]}

    # ── Check for missing ESPN Deportes coverage on major sports ────────────
    print(f"\nChecking for ESPN Deportes coverage gaps...", flush=True)
    missing_deportes = check_espn_deportes_coverage(events)
    deportes_missing_count = len(missing_deportes)

    # ── Check for possible network/rights changes on volatile properties ───
    print(f"\nChecking for network/rights-change signals...", flush=True)
    rights_flags = check_network_rights_changes(events, today)
    rights_flags_count = len(rights_flags)

    # ── WWE schedule watch — unconditional headline digest, see docstring ──
    print(f"\nChecking WWE events for schedule drift...", flush=True)
    wwe_digest = check_wwe_schedule_watch(events, today)
    wwe_digest_count = len(wwe_digest)

    # ── League coverage — is an entire league/competition missing? ─────────
    print(f"\nChecking for missing league coverage...", flush=True)
    missing_leagues = check_league_coverage_gaps(events)
    missing_leagues_count = len(missing_leagues)

    # ── Entertainment schedule watch — unconditional digest, see docstring ─
    print(f"\nChecking for network schedule announcements...", flush=True)
    network_digest = check_network_schedule_announcements(today)
    network_digest_count = len(network_digest)

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
            # Google News sometimes returns tangentially-related results for
            # quoted-phrase + OR-clause queries when few exact matches exist
            # (e.g. an unrelated "Greenbrier Classic cancelled" golf headline
            # surfacing for a "TOUR Championship" query) — require the
            # headline to actually reference the event, same check Step 1
            # uses for its broad sweep.
            if is_flagged(h["title"]) and match_event_in_headline(event["title"], h["title"]):
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

        issues = (len(flagged) + deportes_missing_count + rights_flags_count
                  + missing_leagues_count)

        if not issues:
            f.write("✅  All clear — no issues found. Calendar looks good.\n")
        else:
            f.write(f"⚠️  {issues} issue(s) flagged for review:\n\n")

            if missing_leagues:
                f.write("🚨 LEAGUES WITH ZERO CALENDAR PRESENCE (verify Fubo carriage, then add):\n")
                for league in missing_leagues:
                    f.write(f"  ▸ {league}\n")
                f.write("\n")

            if rights_flags:
                f.write("📡 POSSIBLE NETWORK / RIGHTS CHANGES:\n")
                for item in rights_flags:
                    f.write(f"  ▸ {item['title']}\n")
                    f.write(f"    Date: {item['date']}  |  Calendar has: {item['network']}\n")
                    f.write(f"    Headline: {item['headline']}\n")
                    f.write(f"    → {item['link']}\n")
                f.write("\n")

            if missing_deportes:
                f.write("📺 MISSING ESPN DEPORTES COVERAGE:\n")
                for item in missing_deportes:
                    f.write(f"  ▸ {item['title']} ({item['sport']})\n")
                    f.write(f"    Date: {item['date']}  |  Current: {item['network'] or 'None'}\n")
                f.write("\n")

            if flagged:
                f.write("📰 POTENTIALLY CANCELLED/RESCHEDULED EVENTS:\n")
                for title, data in sorted(flagged.items(),
                                          key=lambda x: x[1]["event"]["date"]):
                    ev = data["event"]
                    f.write(f"▸ {title}\n")
                    f.write(f"  Date: {ev['date']}  |  Network: {ev['network']}\n")
                    for hit in data["hits"][:5]:
                        f.write(f"  [{hit['source']}] {hit['title']}\n")
                        f.write(f"  → {hit['link']}\n")
                    f.write("\n")

        # Always written, regardless of issues — unfiltered digest, not an
        # alarm. See check_wwe_schedule_watch() docstring for why this
        # category gets unconditional visibility instead of keyword gating.
        if wwe_digest:
            f.write("\n" + "-" * 60 + "\n")
            f.write("🤼 WWE EVENTS — RECENT COVERAGE (skim for schedule drift):\n\n")
            for item in wwe_digest:
                f.write(f"  ▸ {item['title']} — calendar has {item['date']} on {item['network']}\n")
                for h in item["headlines"]:
                    f.write(f"      [{h['source']}] {h['title']}\n")
                f.write("\n")

        if network_digest:
            f.write("\n" + "-" * 60 + "\n")
            f.write("📺 NETWORK SCHEDULE ANNOUNCEMENTS (skim for new/moved shows):\n\n")
            for item in network_digest:
                f.write(f"  ▸ {item['network']}\n")
                for h in item["headlines"]:
                    f.write(f"      [{h['source']}] {h['title']}\n")
                f.write("\n")

        f.write(f"\nCompleted: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    return (log_path, len(flagged), deportes_missing_count, rights_flags_count,
            wwe_digest_count, missing_leagues_count, network_digest_count)


if __name__ == "__main__":
    (log_path, flagged_count, deportes_count, rights_count, wwe_count,
     leagues_missing_count, network_count) = run_audit()
    total_issues = flagged_count + deportes_count + rights_count + leagues_missing_count

    if leagues_missing_count:
        msg = f"{leagues_missing_count} league(s) with ZERO calendar presence — open audit log"
    elif rights_count:
        msg = f"{rights_count} possible network/rights change(s) found — open audit log"
    elif deportes_count:
        msg = f"{deportes_count} ESPN Deportes coverage gap(s) found — open audit log"
    elif flagged_count:
        msg = f"{flagged_count} event(s) may need attention — open audit log to review"
    else:
        msg = "Weekly audit complete — calendar is all clear ✅"
    # wwe_count / network_count intentionally excluded from the
    # notification message — both are passive digests (near-constant news
    # volume would make every week say "found something" and train the
    # notification to be ignored). Headlines are always in the log for
    # anyone who wants to skim them.

    subprocess.run(["osascript", "-e",
        f'display notification "{msg}" with title "fubo Calendar Audit" sound name "Glass"'])

    print(f"\n[{date.today()}] {msg}", flush=True)
    print(f"Log saved: {log_path}", flush=True)
