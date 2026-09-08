"""NFL.com official injury report — OFFICIAL tier (source of truth).

Wed–Fri practice participation plus the Fri game-status designation
(Out / Doubtful / Questionable). Likely an HTML scrape (selectolax); isolate
it well — the markup drifts. Not implemented yet (brief deliverable #4).
"""

from __future__ import annotations

from models import Config, SourceResult, SourceTier
from sources.base import PoliteSession, fetch_guard

SOURCE = "nfl_official"
TIER = SourceTier.OFFICIAL


@fetch_guard(SOURCE, TIER)
def fetch(config: Config, session: PoliteSession) -> SourceResult:
    return SourceResult.failed(
        SOURCE, TIER, "adapter not implemented yet (brief deliverable #4)"
    )
