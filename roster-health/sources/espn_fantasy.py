"""ESPN Fantasy API — league-true projections (deliverable "Level 2").

The problem this solves
-----------------------
The *public* espn.com player pages carry generic, PPR-flavored numbers that do
NOT match a specific league's scoring — that's the "skewed data" you can't
trust. But the ESPN **Fantasy app** doesn't run on that; it runs on a private
Fantasy API (``lm-api-reads.fantasy.espn.com``) that scores everything with
*your* league's exact rules. This module taps that backend directly.

Why compute instead of just reading ESPN's number
--------------------------------------------------
The Fantasy API hands back both a raw projected *stat line* (yards, TDs,
receptions, ...) and its own pre-computed point total (``appliedTotal``). We
pull your league's scoring rulebook (``scoringItems``: statId -> points, with
position overrides) and re-score the raw stat line ourselves:

    projection = Σ  raw_stat[statId] × league_points(statId, position)

This makes the number **independent of whatever ESPN chooses to display**. It is
immune to the display/scoring skew you were seeing, and it self-documents (we
can show the per-stat breakdown). Where ESPN hasn't posted a projected stat line
yet (classically D/ST early in the week), there is genuinely nothing to score —
we report that honestly as "no projection posted" rather than inventing a 0.

Everything here fails soft: any auth / network / shape problem logs a warning
and returns ``None`` so callers fall back to their previous behavior. ESPN is
unreachable from the build sandbox; this runs on GitHub Actions / a local box
where the Fantasy API is reachable.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from models import Config
from normalize import canonical_team

log = logging.getLogger("roster-health.espn_fantasy")

# Modern read replica used by the app + fantasy.espn.com. Path-style league id.
_BASE = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
    "seasons/{year}/segments/0/leagues/{league_id}"
)

# statSourceId on a stat entry: 0 = actual (real), 1 = projected.
_STAT_SOURCE_ACTUAL = 0
_STAT_SOURCE_PROJECTED = 1

# ESPN defaultPositionId -> our position vocabulary (this is the *player*
# position scheme, NOT the lineup-slot scheme). Used for display.
_POSITION_ID = {
    1: "QB",
    2: "RB",
    3: "WR",
    4: "TE",
    5: "K",
    16: "DST",
}

# pointsOverrides in scoringItems are keyed by LINEUP-SLOT id, which differs
# from defaultPositionId for everything except D/ST (16 in both). Convert a
# player's defaultPositionId to the slot id before looking up an override.
#   defaultPositionId:  1 QB  2 RB  3 WR  4 TE  5 K   16 D/ST
#   lineup-slot id:      0 QB  2 RB  4 WR  6 TE  17 K  16 D/ST
_DEFPOS_TO_SLOT = {1: 0, 2: 2, 3: 4, 4: 6, 5: 17, 16: 16}

# statId -> human label. ONLY for pretty-printing the breakdown; the scoring
# math never needs it (it multiplies by statId directly). Unknown ids print as
# "stat<N>". Values follow ESPN's published scoring-format map.
_STAT_LABEL = {
    3: "pass yds", 4: "pass TD", 19: "pass 2pt", 20: "INT thrown",
    24: "rush yds", 25: "rush TD", 26: "rush 2pt",
    42: "rec yds", 43: "rec TD", 44: "rec 2pt", 53: "receptions",
    72: "fumbles lost",
    74: "made FG 50+", 77: "made FG 40-49", 80: "made FG <40",
    85: "missed FG", 86: "made XP", 88: "missed XP",
    # Defense / special teams — scoring events
    93: "blk kick ret TD", 94: "def ret TD", 95: "def INT",
    96: "fumble rec", 97: "def blocked kick", 98: "def safety",
    99: "def sack", 101: "kick return TD", 102: "punt return TD",
    103: "int ret TD", 104: "fumble ret TD",
    # Defense — points-allowed tiers
    89: "0 pts allowed", 90: "1-6 pts allowed", 91: "7-13 pts allowed",
    92: "14-17 pts allowed", 121: "18-21 pts allowed", 122: "22-27 pts allowed",
    123: "28-34 pts allowed", 124: "35-45 pts allowed", 125: "46+ pts allowed",
    120: "pts allowed", 187: "D/ST pts allowed",
    # Defense — yards-allowed tiers
    127: "yds allowed", 128: "def <100 yds", 129: "def 100-199 yds",
    130: "def 200-299 yds", 131: "def 300-349 yds", 132: "def 350-399 yds",
    133: "def 400-449 yds", 134: "def 450-499 yds", 135: "def 500-549 yds",
    136: "def 550+ yds",
}


def _stat_label(stat_id: int) -> str:
    return _STAT_LABEL.get(stat_id, f"stat{stat_id}")


def _as_int(value: Any) -> Optional[int]:
    """Coerce a possibly-stringified integer to int, else None. ESPN mostly
    sends numeric ids as ints but occasionally as strings; scoringPeriodId in
    particular must match reliably or a player wrongly reads as 'no projection'.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Scoring rulebook
# --------------------------------------------------------------------------- #
@dataclass
class ScoringRules:
    """Your league's scoring, straight from ``settings.scoringSettings``.

    ``default[statId]`` is the base point value; ``overrides[statId][posId]`` is
    a position-specific value (ESPN's ``pointsOverrides``) that wins when the
    scored player plays that position.
    """

    default: dict[int, float] = field(default_factory=dict)
    overrides: dict[int, dict[int, float]] = field(default_factory=dict)

    def points_for(self, stat_id: int, slot_id: int | None) -> float:
        # pointsOverrides are keyed by lineup-slot id, so slot_id must be a
        # slot id (see _DEFPOS_TO_SLOT), not a raw defaultPositionId.
        if slot_id is not None:
            ov = self.overrides.get(stat_id)
            if ov and slot_id in ov:
                return ov[slot_id]
        return self.default.get(stat_id, 0.0)

    def score(self, stat_line: dict[int, float], slot_id: int | None) -> float:
        """Dot-product the raw stat line against the rulebook."""
        return sum(
            value * self.points_for(stat_id, slot_id)
            for stat_id, value in stat_line.items()
        )

    @property
    def usable(self) -> bool:
        return bool(self.default) or bool(self.overrides)


# --------------------------------------------------------------------------- #
# One player's projection
# --------------------------------------------------------------------------- #
@dataclass
class PlayerProjection:
    player_id: str
    name: str
    position: str
    week: int
    slot_id: Optional[int] = None   # lineup-slot id (for pointsOverrides resolution)
    stat_line: dict[int, float] = field(default_factory=dict)  # raw projected stats
    computed_points: Optional[float] = None   # OUR Σ from the league rulebook
    espn_points: Optional[float] = None        # ESPN's own appliedTotal (for cross-check)

    @property
    def has_projection(self) -> bool:
        """True when ESPN has actually posted a projection to work from."""
        return self.computed_points is not None or self.espn_points is not None

    @property
    def points(self) -> Optional[float]:
        """The number to trust: our league-computed value, else ESPN's total."""
        if self.computed_points is not None:
            return self.computed_points
        return self.espn_points

    def breakdown(self, scoring: ScoringRules, top: int = 6) -> str:
        """Human-readable 'where the points come from', biggest contributors first."""
        parts = []
        for stat_id, value in self.stat_line.items():
            pts = value * scoring.points_for(stat_id, self.slot_id)
            if abs(pts) >= 0.05:
                parts.append((pts, f"{_stat_label(stat_id)} {value:g}→{pts:+.1f}"))
        parts.sort(key=lambda t: -abs(t[0]))
        return ", ".join(p for _, p in parts[:top]) or "(no scoring stats projected)"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _credentials(config: Config) -> tuple[Optional[str], Optional[str]]:
    espn = config.espn
    swid = os.environ.get("ESPN_SWID") or espn.swid
    s2 = os.environ.get("ESPN_S2") or espn.espn_s2
    return swid, s2


def configured(config: Config) -> bool:
    swid, s2 = _credentials(config)
    return bool(config.espn.league_id and swid and s2)


def _api_get(config: Config, views: list[str], filter_header: dict | None = None) -> Any:
    import requests  # lazy: only when actually hitting ESPN

    swid, s2 = _credentials(config)
    url = _BASE.format(year=config.espn.year, league_id=config.espn.league_id)
    headers = {
        "User-Agent": config.politeness.user_agent,
        "Accept": "application/json",
    }
    if filter_header is not None:
        headers["x-fantasy-filter"] = json.dumps(filter_header)
    resp = requests.get(
        url,
        params=[("view", v) for v in views],
        headers=headers,
        cookies={"SWID": swid or "", "espn_s2": s2 or ""},
        timeout=25.0,
    )
    resp.raise_for_status()
    return resp.json()


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_scoring(settings_json: dict) -> ScoringRules:
    """Pull scoringItems out of an ``mSettings`` response into a ScoringRules."""
    rules = ScoringRules()
    items = (
        (settings_json.get("settings") or {})
        .get("scoringSettings", {})
        .get("scoringItems", [])
    )
    for item in items:
        try:
            stat_id = int(item["statId"])
        except (KeyError, TypeError, ValueError):
            continue
        pts = item.get("points")
        if isinstance(pts, (int, float)):
            rules.default[stat_id] = float(pts)
        overrides = item.get("pointsOverrides") or {}
        if isinstance(overrides, dict) and overrides:
            parsed = {}
            for pos_key, val in overrides.items():
                try:
                    parsed[int(pos_key)] = float(val)
                except (TypeError, ValueError):
                    continue
            if parsed:
                rules.overrides[stat_id] = parsed
    return rules


def _projected_stat_line(player: dict, week: int, year: int) -> dict[int, float]:
    """Extract the raw PROJECTED stat map (statId->value) for the given week.

    ESPN carries many stat entries per player (actual + projected, per week and
    per split). We want the one tagged projected (statSourceId=1) for this
    scoring period. Prefer a week-specific entry; fall back to the season
    projection split if no weekly one is present.
    """
    for e in player.get("stats") or []:
        if not isinstance(e, dict):
            continue
        if e.get("statSourceId") != _STAT_SOURCE_PROJECTED:
            continue
        # statSplitTypeId 2 is an undocumented secondary split ESPN's own
        # clients skip; ignore it. 0 = season, 1 = single week.
        if e.get("statSplitTypeId") == 2:
            continue
        stats = e.get("stats")
        if not isinstance(stats, dict) or not stats:
            continue
        # ONLY the entry for this exact week is a weekly projection. A season
        # split (scoringPeriodId 0) carries season *totals*; scoring those as a
        # weekly number would be ~15x too high. We deliberately do NOT fall back
        # to it — no weekly entry means "not posted yet" (an honest None
        # upstream), never a fabricated number.
        if _as_int(e.get("scoringPeriodId")) != week:
            continue
        out: dict[int, float] = {}
        for k, v in stats.items():
            try:
                out[int(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return out
    return {}


def _espn_applied_total(player: dict, week: int) -> Optional[float]:
    """ESPN's own league-scored projected total for the week (for cross-check)."""
    for e in player.get("stats") or []:
        if not isinstance(e, dict):
            continue
        if e.get("statSourceId") != _STAT_SOURCE_PROJECTED:
            continue
        if e.get("statSplitTypeId") == 2:
            continue
        if _as_int(e.get("scoringPeriodId")) == week:
            total = e.get("appliedTotal")
            if isinstance(total, (int, float)):
                return float(total)
    return None


def _build_projection(player: dict, scoring: ScoringRules, week: int, year: int) -> PlayerProjection:
    pos_id = player.get("defaultPositionId")
    pos_id = int(pos_id) if isinstance(pos_id, (int, float)) else None
    # pointsOverrides are keyed by lineup-slot id; convert from the player's
    # defaultPositionId so position-specific scoring (chiefly D/ST) resolves.
    slot_id = _DEFPOS_TO_SLOT.get(pos_id, pos_id) if pos_id is not None else None
    stat_line = _projected_stat_line(player, week, year)
    espn_total = _espn_applied_total(player, week)
    computed = scoring.score(stat_line, slot_id) if (stat_line and scoring.usable) else None
    return PlayerProjection(
        player_id=str(player.get("id", "")),
        name=str(player.get("fullName") or player.get("name") or "?"),
        position=_POSITION_ID.get(pos_id, "?") if pos_id is not None else "?",
        slot_id=slot_id,
        week=week,
        stat_line=stat_line,
        computed_points=round(computed, 2) if computed is not None else None,
        espn_points=round(espn_total, 2) if espn_total is not None else None,
    )


def _iter_roster_players(rostered_json: dict):
    """Yield each player dict from an mRoster response (all teams)."""
    for team in rostered_json.get("teams") or []:
        entries = (team.get("roster") or {}).get("entries") or []
        for entry in entries:
            player = (entry.get("playerPoolEntry") or {}).get("player")
            if isinstance(player, dict):
                yield player


# --------------------------------------------------------------------------- #
# Module-level memo (one process = one weekly pull, even across callers)
# --------------------------------------------------------------------------- #
_CACHE: dict[tuple, Any] = {}


def _current_week(config: Config, base_json: dict | None = None) -> Optional[int]:
    if base_json is not None:
        wk = _as_int(base_json.get("scoringPeriodId"))
        if wk is not None:
            return wk
    try:
        data = _api_get(config, ["mStatus"])
    except Exception:  # noqa: BLE001
        return None
    wk = _as_int(data.get("scoringPeriodId"))
    if wk is not None:
        return wk
    status = data.get("status") or {}
    return _as_int(status.get("latestScoringPeriod") or status.get("currentMatchupPeriod"))


def get_scoring(config: Config) -> Optional[ScoringRules]:
    """Fetch and cache the league's scoring rulebook."""
    if not configured(config):
        return None
    key = ("scoring", config.espn.league_id, config.espn.year)
    if key in _CACHE:
        return _CACHE[key]
    try:
        data = _api_get(config, ["mSettings"])
        rules = parse_scoring(data)
        if not rules.usable:
            log.warning("league %s returned no scoringItems", config.espn.league_id)
            return None
        _CACHE[key] = rules
        log.info("loaded %d scoring rules for league %s", len(rules.default), config.espn.league_id)
        return rules
    except Exception as exc:  # noqa: BLE001
        log.warning("could not load ESPN scoring settings (%s)", exc)
        return None


def projections_by_espn_id(
    config: Config, week: int | None = None, include_free_agents: bool = True
) -> Optional[dict[str, PlayerProjection]]:
    """League-true projections keyed by ESPN player id, for every rostered player
    (all teams) and, optionally, the free-agent pool. ``None`` if unavailable.
    """
    if not configured(config):
        return None
    scoring = get_scoring(config)
    if scoring is None:
        return None

    key = ("proj", config.espn.league_id, config.espn.year, week, include_free_agents)
    if key in _CACHE:
        return _CACHE[key]

    try:
        rostered = _api_get(config, ["mRoster"])
    except Exception as exc:  # noqa: BLE001
        log.warning("ESPN mRoster pull failed (%s)", exc)
        return None

    wk = week or _current_week(config, rostered)
    if wk is None:
        log.warning("could not determine current scoring period; skipping projections")
        return None

    out: dict[str, PlayerProjection] = {}
    for player in _iter_roster_players(rostered):
        proj = _build_projection(player, scoring, wk, config.espn.year)
        if proj.player_id:
            out[proj.player_id] = proj

    fa_ok = True
    if include_free_agents:
        try:
            # Weekly-projection stat id, e.g. week 7 of 2026 -> "1120267"
            # (1=projected, 1=weekly split, season, week). Requesting it in
            # additionalValue makes ESPN include the projected entry in stats.
            weekly_proj_id = f"11{config.espn.year}{wk}"
            fa_filter = {
                "players": {
                    "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
                    "limit": 300,
                    "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
                    "filterStatsForTopScoringPeriodIds": {
                        "value": 2,
                        "additionalValue": [
                            f"00{config.espn.year}",   # season actual
                            f"10{config.espn.year}",   # season projected
                            weekly_proj_id,            # this week's projection
                        ],
                    },
                }
            }
            fa = _api_get(
                config, ["kona_player_info"], filter_header=fa_filter
            )
            for player in (fa.get("players") or []):
                pj = player.get("player")
                if isinstance(pj, dict):
                    proj = _build_projection(pj, scoring, wk, config.espn.year)
                    if proj.player_id and proj.player_id not in out:
                        out[proj.player_id] = proj
        except Exception as exc:  # noqa: BLE001
            fa_ok = False
            log.warning("ESPN free-agent projection pull failed (%s); roster projections still returned", exc)

    # Cache only a complete result: never a sticky empty dict (a transient
    # empty mRoster would otherwise poison later calls), and never a partial
    # (FA pull failed) result under the FA-inclusive key.
    if out and (not include_free_agents or fa_ok):
        _CACHE[key] = out
    log.info("computed league-true projections for %d players (week %s)", len(out), wk)
    return out


def projection_points(
    config: Config, week: int | None = None
) -> Optional[dict[str, float]]:
    """Convenience: ``{espn_player_id: league_points}`` for players that actually
    have a posted projection. Used to enrich roster/free-agent projections.
    """
    projs = projections_by_espn_id(config, week)
    if projs is None:
        return None
    return {pid: p.points for pid, p in projs.items() if p.points is not None}


# --------------------------------------------------------------------------- #
# Standalone CLI: prove the numbers match your league (and spot ESPN skew)
# --------------------------------------------------------------------------- #
def build_compare(config: Config, week: int | None = None) -> str:
    """A readable table of YOUR starters: league-computed vs ESPN-displayed, so
    you can confirm the math matches the app and catch any skew.
    """
    scoring = get_scoring(config)
    if scoring is None:
        return ("ESPN Fantasy API not reachable / not configured — set ESPN_SWID, "
                "ESPN_S2 and the league id. (Runs on GitHub Actions where ESPN is "
                "reachable.)")
    projs = projections_by_espn_id(config, week)
    if not projs:
        return "No projections returned from the ESPN Fantasy API."

    lines: list[str] = ["League-true projections (computed from YOUR scoring rulebook)", ""]
    total_ours = total_espn = 0.0
    shown = 0
    for entry in config.starters:
        pid = entry.player_id_espn
        proj = projs.get(pid) if pid else None
        if proj is None:
            # match by name as a fallback for config-only rosters
            proj = next(
                (p for p in projs.values()
                 if p.name.lower().split()[0:2] == entry.name.lower().split()[0:2]),
                None,
            )
        label = f"{entry.name} ({entry.team} {entry.position})"
        if proj is None or not proj.has_projection:
            lines.append(f"  {label}: no projection posted yet")
            continue
        ours = proj.computed_points
        espn = proj.espn_points
        shown += 1
        if ours is not None:
            total_ours += ours
        if espn is not None:
            total_espn += espn
        skew = ""
        if ours is not None and espn is not None and abs(ours - espn) >= 0.5:
            skew = f"  ⚠ diff {ours - espn:+.1f}"
        ours_s = f"{ours:.1f}" if ours is not None else "—"
        espn_s = f"{espn:.1f}" if espn is not None else "—"
        lines.append(f"  {label}: {ours_s} pts (ESPN says {espn_s}){skew}")

    lines += [
        "",
        f"  Starter total — ours: {total_ours:.1f} pts | ESPN: {total_espn:.1f} pts"
        + (f" (diff {total_ours - total_espn:+.1f})" if shown else ""),
        "",
        "Numbers are computed from your league's exact scoring settings, not the "
        "generic espn.com stats. Where they differ from ESPN's shown total, trust "
        "these — they follow your rulebook.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(
        description="Show league-true projections from the ESPN Fantasy API"
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--week", type=int, default=None, help="scoring period (default: current)")
    parser.add_argument("--push", action="store_true", help="also send to your phone")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    import sys
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    from monitor import load_config

    config = load_config(args.config)
    text = build_compare(config, args.week)
    print(text)

    if args.push:
        from notify import make_notifier
        try:
            make_notifier(config.notify, dry_run=False).send(
                "League Projections", text, url="https://fantasy.espn.com/"
            )
            print(f"\n(pushed via {config.notify.provider})")
        except Exception as exc:  # noqa: BLE001
            print(f"\n(push failed via {config.notify.provider}: {exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
