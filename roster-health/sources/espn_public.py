"""ESPN public news / injury endpoints — CROSS_CHECK tier.

Distinct from the private-league API in ``espn_roster.py``: this is the public
site API (site.api.espn.com) for player news and injury designations. No auth.
Not implemented yet (brief deliverable #4).
"""

from __future__ import annotations

from models import Config, SourceResult, SourceTier
from sources.base import PoliteSession, fetch_guard

SOURCE = "espn_public"
TIER = SourceTier.CROSS_CHECK


@fetch_guard(SOURCE, TIER)
def fetch(config: Config, session: PoliteSession) -> SourceResult:
    return SourceResult.failed(
        SOURCE, TIER, "adapter not implemented yet (brief deliverable #4)"
    )
