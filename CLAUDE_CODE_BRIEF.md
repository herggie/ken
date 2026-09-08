# Build Brief: Fantasy Football Roster Health Monitor

## What I'm building
A background service that monitors my fantasy football roster's player health/status across
multiple sources and pushes me an alert when something changes or when sources disagree.
It replaces manually asking for a "Team Health Check" every week.

I want you (Claude Code) to scaffold the project, write the core, and help me iterate. Ask me
for any secrets/config values you need rather than guessing them.

## My context
- League: ESPN, 12-team, 1-point PPR. Private league (needs cookie auth).
- I'm comfortable in the shell/Python; I've run kubeadm clusters, CSI drivers, cron jobs.
  Don't over-explain basics; do explain anything ESPN-cookie or scraping-specific.
- I want this runnable without my PC on — prefer a scheduler that can run in the cloud
  (GitHub Actions on a timer) with a local cron fallback documented.

## Architecture (keep these concerns separated)

### 1. Roster source (one)
- ESPN private league via the `espn-api` Python library.
- Purpose: know which players I own. Auth via `SWID` + `espn_s2` cookies + league ID + team ID.
- This changes rarely; cache it and allow a manual refresh.

### 2. Status/news sources (multiple, pluggable)
Put EACH source in its own adapter file under `sources/`, each returning the SAME normalized
record shape (see below). One source failing must NEVER crash the run — wrap each in its own
try/except and emit a structured "source failed" record instead.

Official / authoritative tier (source of truth):
- `nfl_official.py`   — NFL.com official injury report (Wed–Fri participation + game-status
                         designations: Out/Doubtful/Questionable; Friday final).
- `nfl_inactives.py`  — NFL game-day inactives list (~90 min before kickoff): definitive
                         "not playing today" signal.
- `team_sites.py`     — the official team sites for teams I have players on (e.g.
                         commanders.com, 49ers.com, etc.): team injury reports, depth charts,
                         beat posts. Likely RSS where available, HTML scrape otherwise.

Cross-check / early-signal tier:
- `sleeper.py`        — Sleeper public API (`/players/nfl`): structured injury + depth-chart
                         data. Stable, no auth. Good primary structured feed.
- `espn_public.py`    — ESPN public news/injury endpoints (separate from the private-league API).
- `news_rss.py`       — beat-writer / player-news RSS (Rotoworld/NBC + team beat reporters):
                         catches role changes and surprise scratches structured feeds miss.

### 3. Normalized record shape
Every adapter returns a list of records like:
```
{
  "player_id_espn": "...",     # when known
  "player_name": "...",
  "team": "WSH",
  "status": "Questionable",    # normalized enum: Active|Questionable|Doubtful|Out|IR|Unknown
  "role_note": "RB1 / lead back",   # optional, when the source gives depth-chart info
  "raw": "...",                # original text from the source
  "source": "nfl_official",
  "source_tier": "official",   # official | cross_check
  "url": "...",
  "timestamp": "2026-09-08T14:03:00Z"
}
```

### 4. ID crosswalk (`id_crosswalk.py`)
Map players across ESPN / Sleeper / NFL IDs. Sleeper's player file includes ESPN IDs for many
players — use that as the bridge. Persist the crosswalk to disk and only rebuild when a player
can't be resolved. Provide a fuzzy name+team fallback for stragglers, and LOG unresolved
players rather than silently dropping them.

### 5. Reconciliation (`reconcile.py`)
- Join all status records to my roster via the crosswalk.
- Treat the official tier (NFL.com, NFL inactives, team sites) as source of truth.
- Use cross-check tier as early warning + disagreement detection.
- Emit an ALERT when, for any of my players:
  - the official designation downgrades (Active→Q→D→Out, or added to inactives), OR
  - a cross-check source disagrees with the official one (show both + timestamps), OR
  - a depth-chart/role change is detected (e.g. dropped from starter).
- For any flagged starter, ALSO surface my healthy bench options at the same position
  (I'll provide roster slots in config so you know starters vs bench).
- Produce a concise digest: per-player one-liner (status, source, note), sorted so problems
  are at the top.

### 6. Notification (`notify.py`)
- Pluggable: support ntfy, Pushover, and a Discord/Slack webhook. Start with a stub + ntfy.
- Fire ONLY on change or disagreement vs. the last run (persist last-run state to disk and diff).
- Include the concise digest in the push; link to sources.

### 7. Scheduler
- Primary: GitHub Actions workflow on a cron schedule (so my PC needn't be on).
- Game-day-aware timing: a few times daily Wed–Sat, then tighter Sunday morning through the
  inactives window (~90 min pre-kickoff), lighter otherwise.
- Document a local `cron` fallback equivalent.

### 8. Config (`config.example.yaml`)
- ESPN: league_id, team_id, SWID, espn_s2 (I'll fill these in; keep them out of git).
- My roster with slots (QB/RB/RB/WR/WR/TE/FLEX/FLEX/DST/K/BENCH) so reconcile knows
  starters vs bench. Allow easy weekly edits for waivers/trades.
- Which teams' official sites to watch (derive from my roster automatically, but allow override).
- Notification provider + credentials (keep out of git; use env vars / GH secrets).
- Politeness: per-source rate limits, cache TTLs, user-agent string.

## Hard requirements / gotchas to respect
- The ESPN private-league API is unofficial and can break — isolate it and fail gracefully.
- Team-site / NFL.com scrapers are brittle: isolate each adapter, add a per-source health check,
  and make a broken scraper degrade the run (flag "source unavailable") rather than kill it.
- Respect robots.txt and rate-limit politely (cache, space requests, real user-agent). A check
  every few hours is plenty; only tighten on game-day mornings.
- Secrets never committed. Use env vars locally and GitHub Actions secrets in the cloud.
- Injury feeds are only as fresh as their publisher; treat NFL inactives as the last word on
  game day and keep the ESPN/Sleeper app pushes as a human backup (document this).

## Deliverables I want from you, in order
1. Project skeleton (folders + empty adapter files + README).
2. `sleeper.py` first (no auth, easiest to validate end-to-end), then the normalizer + crosswalk,
   so we can prove the pipeline with one source before adding the brittle ones.
3. `espn_roster.py` (roster pull) — you'll ask me for league_id/team_id/cookies.
4. Remaining source adapters (nfl_official, nfl_inactives, team_sites, espn_public, news_rss).
5. `reconcile.py` + `notify.py` (ntfy stub) + last-run diff.
6. GitHub Actions workflow + local cron docs.
7. A short README: setup, where to put secrets, how to edit my roster weekly, how to add a new
   source (one new adapter file).

## Tech preferences
- Python 3.12. `requests`/`httpx`, `feedparser` for RSS, `beautifulsoup4`/`selectolax` for HTML,
  `pydantic` for the normalized record model, `PyYAML` for config. `espn-api` for the roster.
- Keep dependencies lean. Type hints. One adapter = one file = one responsibility.
- Log to stdout (GH Actions captures it) with a `--dry-run` that skips notifications.

## First step
Scaffold the skeleton and build the Sleeper adapter + normalizer + crosswalk, then show me a
`--dry-run` digest for my roster using Sleeper only. Ask me for my league ID, team ID, and the
`SWID`/`espn_s2` cookies when you're ready to wire the roster pull. Then we'll add the official
sources one at a time.
