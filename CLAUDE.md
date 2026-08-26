# fubo-sports-live

A hand-curated HTML calendar of sports + entertainment events available on Fubo (all plans, Fubo Latino, Fubo Canada), auto-updated daily and pushed to GitHub Pages at https://escully-cpu.github.io/fubo-sports-live/.

## Before adding or editing any broadcast network assignment

**Always verify via WebSearch before typing a network from memory — do not trust training-data recall for TV rights.** Broadcast rights change year to year, sometimes mid-deal (rotations), and getting this wrong is the single most common error in this project.

Concrete incidents that motivated this rule (2026-08-17):
- Wrote "TOUR Championship... on NBC" from memory. Actually: the PGA Tour's 2022–2030 media rights deal **rotates the FedEx Cup Playoffs (St. Jude, BMW, TOUR Championship) between NBC and CBS every other year** — 2026 is CBS's year. Same mistake was made for BMW Championship.
- Wrote "BMW Championship... Castle Pines Golf Club, CO" from memory. Actually 2026 venue is Bellerive Country Club, St. Louis, MO. Castle Pines hosted in 2024.
- Added "The Sentry" and "Sony Open in Hawaii" as Jan 2027 events. Both were **dropped from the 2027 PGA Tour schedule** — The American Express is now the season opener instead.
- Wrote "Hero World Challenge... Dec 3–6". Actual dates are Nov 30–Dec 6.

None of these were guessable from internal consistency — they required an actual web search to catch. If a property has ever had its rights change hands (majors, FedEx Cup Playoffs, Ryder/Presidents/Solheim Cup, any "new TV deal" property), search for the **current year's** specific assignment before writing it down, even if you're confident from a prior season.

## TUDN / Univision / UniMás / Galavisión are NOT on Fubo (found 2026-08-26)

**None of the TelevisaUnivision channels are available on Fubo.** They've been off since a **December 2024 carriage dispute** (Fubo balked at a proposed 25% price increase) and were still not restored as of 2026-08. Do not credit any of these as a Fubo-available network — they were wrongly present in `FUBO_LINEAR_NETWORKS`, `_infer_network()`'s regex mapping, and a `PRESS_FEEDS` entry in `auto_update.py` (removed 2026-08-26; that `FUBO_LINEAR_NETWORKS` entry predated this session, so this bug had been live for a while). Ten `index.html` entries had a `latino-pill` and/or "Spanish on TUDN" claim purely because of this — fixed: UEFA Nations League (×3), CONCACAF Nations League (×3), Leagues Cup (×3), AFC Asian Cup. The Liga MX Apertura/Clausura Finals had **no other Fubo network at all** — removed entirely from both `index.html` and `RECURRING_MANIFEST` (there's no Fubo-available way to watch them right now), and Liga MX moved to `LEAGUE_COVERAGE_CHECKLIST`'s excluded-with-reason list so the completeness checker doesn't treat this as a standing false alarm.

Fubo Latino still has ESPN Deportes, FOX Deportes, Telemundo, Universo, and beIN Sports — those remain valid Spanish-language options and don't need this treatment. Before crediting any Spanish-language network as Fubo-available, confirm it's actually one of these, not a TelevisaUnivision property. If this carriage dispute ever resolves, reverse all of the above (search "TUDN Fubo restored" or similar before trusting any single source, since carriage disputes can drag on and get reported as "close to resolution" repeatedly before actually resolving).

## The automated pipeline

Four LaunchAgents run daily/weekly (`~/Library/LaunchAgents/tv.fubo.sports-*.plist`):

| Agent | Script | Schedule | Job |
|---|---|---|---|
| `tv.fubo.sports-autodiscover` | `run_auto_update.sh` → `auto_update.py` | Daily 9:00 AM | Prunes past events, resolves TBD dates, verifies existing dates/times, enriches details, discovers new events (TheSportsDB/TVMaze/RSS), runs `RECURRING_MANIFEST` audit |
| `tv.fubo.sports-update` | `run.sh` → `update_local.py` | Daily 9:00 AM | Simple 14-day-past pruner (lighter/older, mostly superseded by auto_update's pruning) |
| `tv.fubo.sports-push` | `push_to_github.sh` | Daily 9:05 AM | Commits + pushes `index.html` to GitHub Pages |
| `tv.fubo.sports-audit` | `run_audit.sh` → `weekly_audit.py` | **Sunday only** 9:15 AM | Scans news for cancellations/reschedules + ESPN Deportes coverage gaps + network/rights-change signals |

**Do not schedule two writers of `index.html` at the same wall-clock time without a lock.** `update_local.py` and `auto_update.py` both read-modify-write the file; on 2026-08-16 this race produced a 0-byte `index.html` that got committed locally (the push itself failed on a network error, so the live site was never actually affected — caught and rolled back the same day). All four `run_*.sh` scripts now take a shared `mkdir`-based lock at `/tmp/fubo_sports_index.lock` before touching the file — auto_update.py's runtime varies from ~3 to 46+ minutes depending on network conditions, so a fixed time offset between agents is not a reliable substitute for the lock. `push_to_github.sh` also refuses to commit/push if `index.html` looks truncated (< 20KB, missing `</html>`, or fewer than 50 `.item` divs) — this is the last line of defense if anything else goes wrong upstream.

## `RECURRING_MANIFEST` in `auto_update.py`

This list is what the daily audit uses to detect a major recurring event has gone missing and needs re-adding. Keep its `network` field in sync with reality — if you fix a network assignment in `index.html`, fix it here too, or the audit will silently resurrect the wrong value the next time that item happens to be missing. If a property drops off the pro schedule entirely (see Sentry/Sony Open above), remove its manifest entry rather than leaving it to be "helpfully" re-added later.

## Class-coverage gotcha

`sports_classes` / `ent_classes` are redefined separately in three functions in `auto_update.py` (`verify_existing_times`, `resolve_tbd_dates`, `enrich_event_details`). When you add a new CSS class to `index.html` (a new network color, e.g. `disney-e`, `nbc-e`, `college-bb`), you must add it to **all three** sets or that class's items silently stop getting date/time verification. This has bitten us before — grep for `sports_classes\s*=` and `ent_classes\s*=` and check all matches whenever a new class is introduced.

## Don't assume a one-off event recurs annually

WrestlePalooza was added as a recurring September fixture after its 2025 debut. It was actually a **one-time launch special** for WWE's ESPN partnership — WWE never scheduled a 2026 edition, and September had no PLE at all in 2026. If something reads as a "first-ever" / debut / launch event, don't project it forward a year without checking the current official schedule first — a debut is exactly as likely to be one-off as to become a tradition, and there's no way to tell from the event itself which it'll be.

## Self-sufficient news watching (`weekly_audit.py`)

The Sunday audit does NOT just rely on keyword-matched cancellation headlines — that approach has a structural blind spot: it can only catch a change if some article happens to phrase it using one of a finite list of words (`FLAG_WORDS`), and real headlines are far more creatively worded than any list can anticipate ("WWE Backs Off Wrestlepalooza Promise", "Why WWE Pushed Money In The Bank 2026 to October", "When Is WWE's Next 2026 PLE?" — none of these matched the original word list, and the last one doesn't even mention the event by name).

Three layers now exist, in increasing order of how much they trust keyword matching:
1. **Broad RSS sweep + targeted per-event cancellation search** (`FLAG_WORDS`, `match_event_in_headline`) — good for explicit, clearly-worded cancellation/reschedule news. Keep `FLAG_WORDS` broad; a missed real change is worse than an extra log line.
2. **`check_network_rights_changes`** — targeted search for a curated list of historically volatile TV-rights properties (`RIGHTS_SENSITIVE_KEYWORDS`), flagged only on rights-specific language.
3. **`check_wwe_schedule_watch`** — for WWE events specifically (the demonstrated failure category, twice now), skips keyword filtering entirely and just surfaces the freshest 2-3 headlines per event, unconditionally, every week, in a dedicated log section. This is deliberate: no word list will ever be complete, so instead of trying to auto-classify "is this bad news," the system does the legwork of finding candidate coverage and a human does a 10-second skim. This is what makes schedule-drift detection actually self-sufficient instead of requiring someone to think to go check.

If another category shows the same failure pattern (assumed-recurring event never confirmed, or a reschedule that slips past `FLAG_WORDS`), the fix is the same: add it to an unconditional-digest watch list like `check_wwe_schedule_watch`, don't just try to expand `FLAG_WORDS` again — that list has already proven it can't be made exhaustive.

## League-level coverage gaps (`check_league_coverage_gaps`)

Everything above audits the ACCURACY of an entry that's already in the calendar. That structurally cannot catch a whole league being absent — which is exactly what happened on 2026-08-18: MLS and IndyCar had **zero entries anywhere in the file**, despite both being at least partially carried by Fubo (IndyCar fully — all 17 races on broadcast FOX; MLS partially — 34 nationally-broadcast FOX/FS1 games + the MLS Cup Final). Also missing at the same time: Leagues Cup (MLS vs. Liga MX, FOX/FS1). Nobody would have caught these without being told, because every other check only ever looks at events already present.

`check_league_coverage_gaps` in `weekly_audit.py` fixes this: `LEAGUE_COVERAGE_CHECKLIST` is a maintained list of ~30 major leagues/competitions, and the check is pure local string matching (no network calls, can't fail or rate-limit) — any league with zero matching titles anywhere in the calendar gets flagged in the Sunday log as `🚨 LEAGUES WITH ZERO CALENDAR PRESENCE`. A flag means "verify current Fubo carriage via WebSearch," not "definitely add" — some leagues genuinely aren't on Fubo (that's correct and shouldn't be added), and some genuinely don't happen in a given year (Gold Cup is odd-years-only, Copa América is 2024/2028, CONCACAF Champions Cup runs Feb–May and may have already concluded by the time you're reading this — see the comment block right after `LEAGUE_COVERAGE_CHECKLIST` for which leagues are intentionally excluded from the active list and why, and when to reinstate them).

**When adding a new sport/league to `index.html`, add it to `LEAGUE_COVERAGE_CHECKLIST` in the same commit.** Otherwise the checklist itself silently drifts out of sync with what the calendar actually needs to cover, and the whole point of this check — not needing to be told what's missing — quietly stops working.

## TheSportsDB's `searchevents.php` is not a league filter (found 2026-08-20)

`discover_sports()` in `auto_update.py` used to loop over a `LEAGUES` list and call `searchevents.php?e=<league name>` for each, on the assumption this returns "events in that league." **It doesn't.** That endpoint does near-exact string matching against a specific event's own name — it only returns anything if the literal league name string happens to appear inside real event titles. Verified by hand, `s=2026`, every one of these returned **zero**: NFL, NHL, MLB, PGA Tour, Tennis, UEFA Champions League, Copa Libertadores, Copa Sudamericana, NWSL, Liga MX, MLS, EPL, La Liga, Serie A, Bundesliga, Leagues Cup, CONCACAF Nations League, UEFA Nations League. Only NBA, WNBA, NASCAR, and IndyCar returned anything (1 each) — because those leagues' real event names happen to literally contain the league name ("NBA All-Star Game", "NASCAR All-Star Race"). `LEAGUES` is now pruned to just those four; don't add league names back to it expecting it to start working; test with a raw curl against `searchevents.php` before trusting any addition.

The pattern that actually works with this API is `RECURRING_MANIFEST`'s: search for a **specific compound event name** ("NFL Wild Card", "MLS Cup Final", "IndyCar Grand Prix of Monterey") rather than a bare league. That's still not guaranteed to return anything (TheSportsDB's free tier just doesn't have great MLS coverage — "MLS Cup Final" and "Leagues Cup Final" both return zero too), but it's the closest thing to a working query shape for this data source. `LEAGUE_COVERAGE_CHECKLIST` in `weekly_audit.py` remains the actually-reliable mechanism for "is a whole league missing," precisely because it's pure calendar string-matching with zero dependency on TheSportsDB's spotty coverage.

## TVMaze schedule data lags real network announcements (found 2026-08-20)

`discover_tv()` scans TVMaze's daily schedule endpoint for new/season premieres. This only finds a show once TVMaze has actually ingested its specific episode-date data — and that ingestion visibly lags real press coverage by weeks. Confirmed by hand: FOX's full Fall 2026 schedule (premiere dates for Best Medicine, Doc, The Floor, Hell's Kitchen S25, etc., all confirmed via July 2026 press coverage) still returned **zero** FOX episodes from `https://api.tvmaze.com/schedule?country=US&date=2026-09-22` as of 2026-08-20 — a month after the dates were public and a month before they aired.

The press-release RSS scanner (`PRESS_FEEDS`, `discover_press_releases`) DOES find the right headlines — a live test returned "Fall 2026 Premiere Dates: Full Schedule for New & Returning Shows," "ABC Locks in Fall 2026 Premiere Dates," etc., 100 items deep — but `_parse_press_date()` returned `None` for every single one. These are schedule-roundup articles that list each show's date inside the article body in a table/list, not as a single date near the headline text the RSS feed exposes. No regex against the RSS snippet can extract that; it would require fetching and parsing full article bodies, which this pipeline doesn't do.

**The fix is the same philosophy as `check_wwe_schedule_watch`**: `check_network_schedule_announcements` in `weekly_audit.py` doesn't try to extract structured dates from these articles — it just surfaces the freshest "fall schedule" / "premiere dates" headlines per major network (`ENTERTAINMENT_NETWORKS_WATCH`) unconditionally, every week, so a human catches "oh, there's a new FOX schedule roundup, let me check it against the calendar" instead of TVMaze silently staying a month behind. Live-verified: immediately surfaced "2026 Fall TV Schedule, Release Dates: ABC, CBS, Fox, NBC Premieres Calendar" — exactly the useful signal. This is most valuable during premiere-announcement season (roughly May–August for fall, Dec–Jan for midseason) but costs nothing to run year-round.

**Bottom line on "fully self-sufficient" for entertainment**: there is no free-tier mechanism that will auto-insert new shows without a human/AI skim step — TVMaze lags, and RSS-based date extraction from roundup articles doesn't work. What's achievable, and now built, is guaranteeing the right headlines surface every week without anyone having to think to go searching for them. Closing the last mile (reading the digest and actually updating `index.html`) still needs a periodic pass — this is a deliberate trade against blindly auto-inserting unverified show/date/network combinations into a calendar people rely on for accuracy.
