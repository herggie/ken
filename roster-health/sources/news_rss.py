"""Beat-writer / player-news RSS — CROSS_CHECK tier.

Rotoworld/NBC + team beat reporters. Catches role changes and surprise
scratches that the structured feeds miss. ``feedparser`` over a configurable
list of feeds; match items to roster players by name. Not implemented yet
(brief deliverable #4).
"""

from __future__ import annotations

from models import Config, SourceResult, SourceTier
from sources.base import PoliteSession, fetch_guard

SOURCE = "news_rss"
TIER = SourceTier.CROSS_CHECK

# Default feeds, extend/override via config later.
DEFAULT_FEEDS: list[str] = [
    "https://www.nbcsports.com/fantasy/football/player-news/feed",
]


@fetch_guard(SOURCE, TIER)
def fetch(config: Config, session: PoliteSession) -> SourceResult:
    return SourceResult.failed(
        SOURCE, TIER, "adapter not implemented yet (brief deliverable #4)"
    )
