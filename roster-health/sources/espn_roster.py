"""ESPN private-league roster pull.

This is the *roster* source (who do I own), not a status source. It uses the
unofficial ``espn-api`` library with cookie auth (SWID + espn_s2) for a private
league. Because that API is unofficial and can break, it is isolated here and
must fail gracefully — when it can't authenticate, the monitor falls back to
the hand-maintained ``roster`` in config.

Not wired yet: needs league_id / team_id / SWID / espn_s2, which the brief
says to ask for before implementing (deliverable #3). The function signature
and return shape are settled so reconcile/crosswalk don't change when it lands.
"""

from __future__ import annotations

import logging
import os

from models import Config, RosterEntry

log = logging.getLogger("roster-health.espn_roster")

# ESPN lineup slotId -> our slot label. Filled in when the pull is implemented.
_SLOT_MAP = {
    0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "DST", 17: "K",
    23: "FLEX", 20: "BENCH", 21: "IR",
}


def fetch_roster(config: Config) -> list[RosterEntry] | None:
    """Return the live ESPN roster, or ``None`` to signal 'use config roster'.

    Reads secrets from env (ESPN_SWID / ESPN_S2) falling back to config.
    """
    espn = config.espn
    swid = os.environ.get("ESPN_SWID") or espn.swid
    espn_s2 = os.environ.get("ESPN_S2") or espn.espn_s2
    if not (espn.league_id and espn.team_id and swid and espn_s2):
        log.info("ESPN not configured; using roster from config.yaml")
        return None

    # TODO(deliverable #3): implement with espn-api once cookies are provided.
    #   from espn_api.football import League
    #   league = League(league_id=..., year=..., espn_s2=..., swid=...)
    #   team = next(t for t in league.teams if t.team_id == espn.team_id)
    #   return [_to_entry(p) for p in team.roster]
    log.warning("ESPN roster pull not implemented yet; using config roster")
    return None
