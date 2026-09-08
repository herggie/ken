"""Official team sites — OFFICIAL tier.

Injury reports / depth charts / beat posts from the official sites of the
teams I have players on (derived from the roster, overridable via
``config.watch_teams``). RSS where available, HTML scrape otherwise. Each team
should be isolated so one broken site degrades to "unavailable" for that team
only. Not implemented yet (brief deliverable #4).
"""

from __future__ import annotations

from models import Config, SourceResult, SourceTier
from sources.base import PoliteSession, fetch_guard

SOURCE = "team_sites"
TIER = SourceTier.OFFICIAL

# Official-site hostnames per team, filled in as adapters are built.
TEAM_SITES: dict[str, str] = {
    "WAS": "https://www.commanders.com/",
    "SF": "https://www.49ers.com/",
    "PHI": "https://www.philadelphiaeagles.com/",
    # ... extend as needed
}


@fetch_guard(SOURCE, TIER)
def fetch(config: Config, session: PoliteSession) -> SourceResult:
    return SourceResult.failed(
        SOURCE, TIER, "adapter not implemented yet (brief deliverable #4)"
    )
