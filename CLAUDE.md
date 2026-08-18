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
