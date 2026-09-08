"""ESPN private-league roster pull (deliverable #3).

This is the *roster* source — who do I own — not a status source. It uses the
unofficial ``espn-api`` library with cookie auth (SWID + espn_s2) for a private
league. Because that API is unofficial and can break, it is isolated here and
fails gracefully: any error (bad cookies, ESPN outage, library drift) logs a
warning and returns ``None``, which tells the monitor to fall back to the
hand-maintained ``roster`` in config.yaml.

Secrets come from the environment (ESPN_SWID / ESPN_S2 / ESPN_LEAGUE_ID /
ESPN_TEAM_ID), falling back to config.yaml for local convenience.

    from espn_api.football import League
    league = League(league_id, year, espn_s2=..., swid=...)
    team = <team with matching team_id>
    for player in team.roster: ...

Cannot be validated in the build sandbox (ESPN egress is blocked there); it is
exercised on GitHub Actions / a local machine, where ESPN is reachable.
"""

from __future__ import annotations

import logging
import os

from models import Config, RosterEntry
from normalize import canonical_team

log = logging.getLogger("roster-health.espn_roster")

# espn-api reports the slot a player currently occupies as a string.
# Map ESPN's slot labels onto our slot vocabulary.
_SLOT_MAP = {
    "QB": "QB",
    "RB": "RB",
    "WR": "WR",
    "TE": "TE",
    "K": "K",
    "D/ST": "DST",
    "DST": "DST",
    "FLEX": "FLEX",
    "RB/WR": "FLEX",
    "WR/TE": "FLEX",
    "RB/WR/TE": "FLEX",
    "OP": "FLEX",        # superflex (not this league, but harmless)
    "SUPERFLEX": "FLEX",
    "BE": "BENCH",
    "Bench": "BENCH",
    "IR": "IR",
}

_POS_MAP = {"D/ST": "DST", "DST": "DST"}


def _map_slot(lineup_slot: str) -> str:
    slot = _SLOT_MAP.get(lineup_slot)
    if slot is None:
        log.warning("unknown ESPN lineup slot %r; treating as BENCH", lineup_slot)
        return "BENCH"
    return slot


def _projection(player) -> float | None:
    """Best-effort ESPN projected points for ranking bench options.

    espn-api exposes a per-game average projection on the Player object; fall
    back to the season total. Attribute names have drifted across versions, so
    probe defensively and return None if nothing usable is present.
    """
    for attr in ("projected_avg_points", "projected_total_points", "projected_points"):
        val = getattr(player, attr, None)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
    return None


def fetch_roster(config: Config) -> list[RosterEntry] | None:
    """Return the live ESPN roster, or ``None`` to signal 'use config roster'."""
    espn = config.espn
    swid = os.environ.get("ESPN_SWID") or espn.swid
    espn_s2 = os.environ.get("ESPN_S2") or espn.espn_s2
    if not (espn.league_id and espn.team_id and swid and espn_s2):
        log.info("ESPN not configured; using roster from config.yaml")
        return None

    try:
        from espn_api.football import League  # lazy: only needed when configured
    except ImportError:
        log.warning("espn-api not installed (`pip install espn-api`); using config roster")
        return None

    try:
        league = League(
            league_id=int(espn.league_id),
            year=int(espn.year),
            espn_s2=espn_s2,
            swid=swid,
        )
        team = next((t for t in league.teams if t.team_id == int(espn.team_id)), None)
        if team is None:
            log.warning(
                "team_id %s not found in league %s; using config roster",
                espn.team_id, espn.league_id,
            )
            return None

        entries: list[RosterEntry] = []
        for p in team.roster:
            position = _POS_MAP.get(getattr(p, "position", ""), getattr(p, "position", ""))
            entries.append(
                RosterEntry(
                    name=p.name,
                    team=canonical_team(getattr(p, "proTeam", None)),
                    position=position or "UNK",
                    slot=_map_slot(getattr(p, "lineupSlot", "BE")),
                    player_id_espn=str(p.playerId) if getattr(p, "playerId", None) else None,
                    projection=_projection(p),
                )
            )
        log.info("pulled %d players from ESPN team %s", len(entries), espn.team_id)
        return entries or None
    except Exception as exc:  # noqa: BLE001 — ESPN API is unofficial; never crash the run
        log.warning("ESPN roster pull failed (%s); using config roster", exc)
        return None
