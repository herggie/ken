"""NFL game-day inactives (~90 min before kickoff) — OFFICIAL tier.

The definitive "not playing today" signal and the last word on game day.
Not implemented yet (brief deliverable #4).
"""

from __future__ import annotations

from models import Config, SourceResult, SourceTier
from sources.base import PoliteSession, fetch_guard

SOURCE = "nfl_inactives"
TIER = SourceTier.OFFICIAL


@fetch_guard(SOURCE, TIER)
def fetch(config: Config, session: PoliteSession) -> SourceResult:
    return SourceResult.failed(
        SOURCE, TIER, "adapter not implemented yet (brief deliverable #4)"
    )
