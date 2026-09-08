# Fantasy Football Roster Health Monitor

A background service that watches my fantasy roster's player health/status across
multiple sources and pushes an alert when something changes or when sources
disagree. Replaces manually asking for a weekly "Team Health Check".

Built per [`CLAUDE_CODE_BRIEF.md`](../CLAUDE_CODE_BRIEF.md). This first cut proves
the pipeline end-to-end with **Sleeper** as the single feed; the brittle
official-tier sources are stubbed and get built next (see *Status* below).

---

## How it works

```
config.yaml (roster + slots)
        │
        ▼
 roster (ESPN pull  ──► fallback to config roster)
        │
        ▼
 sources/*  ── each isolated; one failing degrades to "unavailable", never crashes
        │        sleeper ✅   espn_public/nfl_official/nfl_inactives/team_sites/news_rss (stubs)
        ▼
 id_crosswalk  ── bridge ESPN ⇄ Sleeper ⇄ NFL ids (Sleeper carries espn_id); fuzzy fallback
        │
        ▼
 reconcile  ── official tier = source of truth; cross-check = early warning + disagreement
        │        flags downgrades, disagreements, role changes; adds bench options for flagged starters
        ▼
 state diff ── compare to last run; notify ONLY on change
        │
        ▼
 notify  ── stub | ntfy | pushover | discord | slack
```

Every adapter returns the same normalized record (see `models.StatusRecord`) and
is wrapped so a dead upstream can't take the run down.

## Quickstart

```bash
cd roster-health
python3 -m venv .venv && . .venv/bin/activate      # optional
pip install -r requirements.txt

# Dry run against the shipped offline Sleeper sample (no network, no secrets):
SLEEPER_PLAYERS_FILE=fixtures/sleeper_players_sample.json \
  python monitor.py --config config.yaml --dry-run -v

# Real run against the live Sleeper API (needs outbound access to api.sleeper.app):
python monitor.py --config config.yaml --dry-run
```

`--dry-run` prints the digest and the notification that *would* be sent, sends
nothing, and does **not** persist last-run state (so the next real run still
fires). Add `--save-state` to persist anyway.

### Sample dry-run digest

```
Roster Health — 2026-09-08 13:28 UTC
4 issue(s) across 14 rostered players.

  🔴 Christian McCaffrey (SF RB, FLEX): Out [sleeper=Out] — RB1 / starter → bench: Jaylen Warren, Tyjae Spears, Chuba Hubbard
  🔴 George Kittle (SF TE, TE): Doubtful [sleeper=Doubtful] — TE1 / starter
  🔴 A.J. Brown (PHI WR, WR): Questionable [sleeper=Questionable] — WR1 / starter → bench: Rome Odunze
  🔴 Brian Robinson Jr. (WSH RB, RB): Questionable [sleeper=Questionable] — RB1 / starter → bench: Jaylen Warren, Tyjae Spears, Chuba Hubbard
  🟢 ... (healthy players) ...
```

> The Sleeper public API (`api.sleeper.app`) is firewalled in the build sandbox,
> so the sample above was produced from `fixtures/sleeper_players_sample.json`
> via `SLEEPER_PLAYERS_FILE`. On a network with access to Sleeper, drop that env
> var and it fetches live (and caches the ~5 MB players file for a day).

## Configuration

`config.yaml` (tracked in git) holds the **roster + non-secret settings**. Edit
the `roster:` list weekly for waivers/trades and commit it. Slots:
`QB | RB | WR | TE | FLEX | DST | K | BENCH | IR`. Add `player_id_espn:` to any
row to skip fuzzy name matching.

### Secrets — never in git

Cookies/tokens come from the environment (a local `.env`, or GitHub Actions
secrets). `config.yaml`'s `espn.swid`/`espn_s2` are only a local fallback and
should be left unset.

| Env var                        | Purpose                                   |
|--------------------------------|-------------------------------------------|
| `ESPN_SWID`, `ESPN_S2`         | ESPN private-league cookies               |
| `ESPN_LEAGUE_ID`, `ESPN_TEAM_ID` | league + team id                        |
| `NOTIFY_PROVIDER`              | `ntfy` / `pushover` / `discord` / `slack` |
| `NTFY_TOPIC`, `NTFY_TOKEN`     | ntfy target + optional auth               |

**Getting the ESPN cookies:** log in to `espn.com` in a browser, open
DevTools → Application → Cookies → `https://www.espn.com`, and copy the values
of `SWID` (keep the `{...}` braces) and `espn_s2` (long URL-encoded string).
They're bearer credentials for your account — treat them like a password, keep
them in `.env`/GH secrets, and rotate by re-logging-in if leaked.

## Game-day lineup alerts

The digest and every notification lead with **LINEUP ACTIONS** — any *starter*
who is **Out / IR / Doubtful** is called out as "🚨 DO NOT START → start
⟨best healthy bench player at that position⟩ instead", so you never leave an
out player in your lineup. Questionable starters stay a lower-priority "watch".

Bench replacements are ranked **healthy-first, then by ESPN projected points**
when the ESPN cookie pull is active (the pick shows `~Npts`); without cookies it
falls back to NFL depth-chart role. FLEX holes can be filled by any RB/WR/TE.
Combined with the game-day-aware schedule (tighter Sunday morning through the
inactives window), this is the "don't start an inactive guy" safety net. Once
the official `nfl_inactives` source lands (deliverable #4), the ~90-min-pre-kick
inactives list makes it definitive.

## Run it locally (turnkey)

On your desktop (Python 3.11+):

```bash
git clone https://github.com/herggie/ken.git      # or: git pull
cd ken/roster-health
git checkout claude/health-monitor-setup-wu7e0b

cp .env.example .env        # then edit .env: paste ESPN_SWID / ESPN_S2
./run_local.sh --dry-run    # installs deps, prints the digest, sends nothing
```

`run_local.sh` sources `.env`, ensures deps are installed, and runs against
`config.yaml`. Start with `--dry-run`; drop it for a real run (notifies on
change, persists `.state/` so it only alerts on *changes* next time).

Your `league_id`/`team_id` are already in `config.yaml`, so the only things
`.env` needs for a live pull are the two cookies.

## Report back to Claude Code

A locally-run script can't push into a Claude Code chat directly, but it can
comment on a PR that a Claude session is watching. Set in `.env`:

```bash
NOTIFY_PROVIDER=github
GITHUB_TOKEN=github_pat_...      # fine-grained token, issues:write on herggie/ken
NOTIFY_GITHUB_ISSUE=1            # PR #1
```

On a run with changes, the digest is posted as a comment on PR #1; the watching
Claude session is woken by that comment and can respond. (For phone alerts
instead, use `NOTIFY_PROVIDER=ntfy` with an `NTFY_TOPIC`.)

## Weekly report

One comprehensive, easy-to-read readout of your whole team:

```bash
python report.py --config config.yaml
```

Four sections in one view:
1. **Lineup (start/sit)** — who not to start + the best replacement, plus a
   Questionable watch list.
2. **Bye-week conflicts** — upcoming weeks where 2+ starters are off, so you can
   plan waivers/trades ahead (byes come from the feed or `config.bye_weeks`).
3. **Waiver ideas** — positions where an Out starter has no healthy bench cover,
   with pickups from **your ESPN league's live free agents** (ranked by ESPN
   projection) when cookies are set; without cookies it falls back to an NFL-wide
   approximation and labels it as such.
4. **IR management** — players you should move to an IR slot (frees a roster
   spot; shows the bench cover + a waiver pickup for the freed spot), and
   players who've come **off** IR and must be activated.

**Get it on your phone:** `python report.py --config config.yaml --push` sends
the whole report through your notifier (ntfy). The `roster-report` GitHub
Actions workflow does this automatically every Sunday morning. IR returns and
"move to IR" nudges also ride along in the monitor's regular change alerts.

## Trade validator

Got a trade offer? Fact-check it before you accept:

```bash
python trade.py --give "Saquon Barkley" --get "Malik Nabers, Xavier Worthy"
# same-name players: add a team hint  ->  "Mike Williams (NYJ)"
```

It prints each player's live **status / position / depth-chart role** (from
Sleeper) and objective flags: the **position change**, whether your **roster
size** changes (so you know if you'll have to drop/add), and whether you're
**acquiring or shipping a dinged player**. Names are fuzzy-matched; anything it
can't find is listed, never silently dropped. It states facts, not a verdict —
the value call stays yours (projection-based value needs the ESPN pull).

Trades aren't always straight-up — add **FAAB/money** and/or **draft picks** on
either side and they're factored into the summary (net FAAB, pick-count delta):

```bash
python trade.py --give "Saquon Barkley" --get "Malik Nabers" \
    --get-faab 15 --give-picks "2027 1st, 2027 3rd"
```

## Logs & troubleshooting

Every run appends to **`.state/monitor.log`** (override with `--log-file`):
run start, each source, the notification outcome, and a completion summary.
Notification failures also print to the console. To debug a push that didn't
arrive, run with `-v` and check the log for the ntfy line:

- `ntfy delivered (HTTP 200) to topic <x>` → the send worked; the problem is
  the phone app's subscription/permissions (subscribe to the **exact** topic).
- `notification FAILED via ntfy: ...` → the send itself failed; the message
  says why (bad topic, network, auth).

## Scheduler

### Primary: GitHub Actions (runs without your PC on)

[`.github/workflows/roster-health.yml`](../.github/workflows/roster-health.yml)
runs on a **game-day-aware** cron: twice daily Wed–Sat, then tighter Sunday
morning through the ~90-min-pre-kickoff inactives window, plus a Monday-night
pregame check. Add the secrets above under *Settings → Secrets and variables →
Actions*. Last-run state is carried between runs via `actions/cache` (best
effort — a cache miss just re-baselines, meaning one quiet run).

### Local cron fallback

`config.yaml`'s state lives in `.state/`, which persists naturally on a local
box, so cron is simpler than CI. Example crontab (times in your local zone):

```cron
# Wed–Sat, 10:00 and 19:00
0 10,19 * * 3-6  cd ~/ken/roster-health && /usr/bin/env ESPN_SWID=... ESPN_S2=... ./.venv/bin/python monitor.py --config config.yaml >> ~/roster-health.log 2>&1
# Sunday, every 30 min from 09:00–13:00 (through inactives)
*/30 9-13 * * 0  cd ~/ken/roster-health && ./.venv/bin/python monitor.py --config config.yaml >> ~/roster-health.log 2>&1
```

Put secrets in a sourced env file rather than inline in crontab. A check every
few hours is plenty; only game-day mornings need to be tight.

## Adding a new source (the whole extension surface)

1. Create `sources/<name>.py` exposing
   `fetch(config, session) -> SourceResult`, decorated with
   `@fetch_guard("<name>", tier)`. Return normalized `StatusRecord`s; on any
   failure the guard emits a structured "source unavailable" result.
2. Register it in `monitor.py`'s `SOURCES` table.
3. Add a toggle under `sources:` in `config.yaml`.

One adapter = one file = one responsibility. That's it.

## Status vs. the brief

| Deliverable | State |
|---|---|
| 1. Skeleton + README | ✅ |
| 2. `sleeper.py` + normalizer + `id_crosswalk.py` | ✅ |
| 3. `espn_roster.py` (roster pull) | ✅ implemented — **set league_id / team_id / SWID / espn_s2 (env) to activate**; falls back to config roster |
| 4. `nfl_official` / `nfl_inactives` / `team_sites` / `espn_public` / `news_rss` | ⏳ stubbed |
| 5. `reconcile.py` + `notify.py` (ntfy) + last-run diff | ✅ |
| 6. GitHub Actions + local cron docs | ✅ |
| 7. README | ✅ |

### Next step

The ESPN roster pull is wired — set `ESPN_LEAGUE_ID` / `ESPN_TEAM_ID` /
`ESPN_SWID` / `ESPN_S2` (env or GH secrets) to activate it; until then the
config roster is used. Next is the official-tier sources (`nfl_official`,
`nfl_inactives`, `team_sites`), added one at a time so reconcile gets a real
source-of-truth tier to compare Sleeper against.

## Layout

```
roster-health/
├── monitor.py           # CLI entrypoint / pipeline
├── models.py            # normalized record + config (pydantic)
├── normalize.py         # status + team-code normalization
├── id_crosswalk.py      # ESPN⇄Sleeper⇄NFL id bridge (+ persistence)
├── reconcile.py         # join, alerts, bench options, digest
├── state.py             # last-run persistence + diff
├── notify.py            # pluggable notifiers
├── sources/
│   ├── base.py          # polite cached HTTP session + fetch_guard
│   ├── sleeper.py       # ✅ implemented (cross-check tier)
│   ├── espn_roster.py   # ✅ roster source (needs ESPN cookies via env)
│   └── ...              # official/cross-check stubs
├── fixtures/            # offline sample data for dev/CI
├── config.yaml          # roster + settings (tracked; NO secrets)
├── config.example.yaml  # template
└── requirements.txt
```
