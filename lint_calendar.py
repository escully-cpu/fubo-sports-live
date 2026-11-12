#!/usr/bin/env python3
"""Deterministic sanity checks for index.html. No network calls.

Catches the mechanical errors that previously reached the live page
(2026-11 gut check): duplicate cards, truncated descriptions, junk titles,
"S2026" suffixes, "Every Wednesday" times on one-off dates, cards sitting in
the wrong month block, near-term "~" dates.

  python3 lint_calendar.py            # print findings, exit 1 on any ERROR
  from lint_calendar import lint      # lint(html, today) -> [(level, msg)]

ERROR  = wrong on its face; push_to_github.sh refuses to push.
WARN   = needs a human look; surfaced in the weekly audit.
"""
import re, sys
from datetime import date

MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
DAYS = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

ALIASES = [
    (r"country music association awards?", "cma awards"),
    (r"academy awards?|\boscars?\b", "oscars"),
    (r"golden globe awards?", "golden globes"),
    (r"grammy awards?", "grammys"),
    (r"saturday night live", "snl"),
    (r"dancing with the stars", "dwts"),
]

def canon(t):
    t = re.sub(r"<[^>]+>", "", t).lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    for pat, c in ALIASES:
        t = re.sub(pat, c, t)
    t = re.sub(r"\b(s\d{1,4}|season \d+|the|awards?|\d{1,3}(st|nd|rd|th)|annual)\b", " ", t)
    return " ".join(t.split())

ITEM_RE = re.compile(
    r'<div class="item ([^"]*)"([^>]*)>\s*<div class="date">([^<]*)</div>\s*'
    r'<div class="title">(.*?)</div>\s*<div class="network">(.*?)</div>', re.S)
MONTH_RE = re.compile(r'<div class="month-label">(\w+) <span>(\d{4})</span>')

def _items(html):
    """Yield (month_idx, year, column, classes, attrs, date, title, sub, network)."""
    ent_at = html.find('<div class="col col-ent">')
    cur = None
    for m in re.finditer(MONTH_RE.pattern + "|" + ITEM_RE.pattern, html, re.S):
        if m.group(1) and m.group(2) and m.group(3) is None:
            cur = (MON.index(m.group(1)[:3]), int(m.group(2)))
            continue
        if cur is None:
            continue
        css, attrs, d, title_html, net = m.group(3), m.group(4), m.group(5), m.group(6), m.group(7)
        sub = re.search(r'<span class="sub">(.*?)</span>', title_html, re.S)
        title = re.sub(r"<div[^>]*>.*?</div>|<span[^>]*>.*?</span>", "", title_html, flags=re.S)
        yield (cur[0], cur[1], "ent" if m.start() > ent_at else "sports", css, attrs,
               d.strip(), re.sub(r"<[^>]+>", "", title).strip(),
               re.sub(r"<[^>]+>", "", sub.group(1)).strip() if sub else "",
               re.sub(r"<[^>]+>", " ", net).strip())

def lint(html, today=None):
    today = today or date.today()
    out, seen = [], {}
    for mi, yr, col, css, attrs, d, title, sub, net in _items(html):
        tag = f"{d} | {title[:50]}"
        dm = re.match(r"~?\s*([A-Z][a-z]{2})[a-z]*\s+(\d{1,2})", d)
        # --- title problems
        if re.search(r"\bS20\d\d\b", title):
            out.append(("ERROR", f"year used as season number: {tag}"))
        if title.endswith((",", ":", "-", "–", "—", "&")) or title.count('"') % 2:
            out.append(("ERROR", f"malformed title: {tag}"))
        # --- truncated description (hard 100-char cut, no ending punctuation)
        if len(sub) == 100 and not sub.endswith((".", "!", "?", ")", "…")):
            out.append(("ERROR", f"description cut off mid-word: {tag}"))
        # --- date vs month block
        if dm and dm.group(1) in MON:
            sm = MON.index(dm.group(1))
            end = re.findall(r"([A-Z][a-z]{2})[a-z]*\s+\d{1,2}", d.split("–")[-1].split("-")[-1]) if re.search(r"[–-]", d) else []
            months = {sm} | ({MON.index(end[-1])} if end and end[-1] in MON else set())
            if mi not in months:
                out.append(("ERROR", f"in {MON[mi]} block but dated {d}: {tag}"))
            # --- weekday mismatch on a "Every <weekday>" time for a single date
            t = re.search(r'data-time="([^"]*)"', attrs)
            if t and not re.search(r"[–-]", d):
                w = re.search(r"Every (\w+)", t.group(1))
                if w and w.group(1) in DAYS:
                    try:
                        wd = date(yr, sm + 1, int(dm.group(2))).weekday()
                        # cards for series list the PREMIERE date; flag only when premiere isn't that weekday
                        if DAYS[wd] != w.group(1):
                            out.append(("ERROR", f"{d} is a {DAYS[wd]} but time says 'Every {w.group(1)}': {tag}"))
                    except ValueError:
                        out.append(("ERROR", f"impossible date {d}: {tag}"))
            # --- near-term approximate dates
            if d.startswith("~"):
                try:
                    dt = date(yr, sm + 1, int(dm.group(2)))
                    if 0 <= (dt - today).days <= 21:
                        out.append(("WARN", f"approximate date within 3 weeks: {tag}"))
                except ValueError:
                    pass
            # --- TV card within 14 days with no airtime
            if col == "ent" and "data-time" not in attrs and not d.startswith("~") and not re.search(r"starz-e|showtime-e|mgm-e|foxnation-e", css):
                try:
                    dt = date(yr, sm + 1, int(dm.group(2)))
                    if 0 <= (dt - today).days <= 14:
                        out.append(("WARN", f"airs within 2 weeks, no airtime: {tag}"))
                except ValueError:
                    pass
        # --- duplicates: same canonical title, or same date+network with shared leading words
        k = canon(title)
        if k:
            key = (k, d.lstrip("~"))
            if key in seen:
                out.append(("ERROR", f"duplicate card: {tag}  (also: {seen[key]})"))
            seen[key] = tag
            for (k2, d2), tag2 in list(seen.items()):
                if d2 == d.lstrip("~") and k2 != k and (k2.startswith(k[:14]) or k.startswith(k2[:14])) and len(k) > 8:
                    out.append(("WARN", f"possible duplicate: {tag}  ~  {tag2}"))
    return out

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "index.html"
    findings = lint(open(path, encoding="utf-8").read())
    for lvl, msg in findings:
        print(f"{lvl:5} {msg}")
    errs = sum(1 for l, _ in findings if l == "ERROR")
    print(f"\n{errs} error(s), {len(findings)-errs} warning(s)")
    sys.exit(1 if errs else 0)
