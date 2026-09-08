"""Shared data models for the roster health monitor.

Two groups live here:

* The *normalized record* every source adapter must emit (`StatusRecord`) plus
  the `SourceResult` envelope that lets one source fail without taking the run
  down with it.
* The strongly-typed `Config` tree loaded from ``config.yaml``.

Everything is pydantic v2 so a malformed config or a malformed source record
fails loudly at the boundary instead of somewhere deep in reconcile.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- #
# Normalized status enum
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    """Normalized availability, ordered best -> worst.

    The ordering matters: reconcile compares two statuses to decide whether a
    player *downgraded* (e.g. Active -> Questionable), so keep the members in
    increasing severity and use :meth:`severity`.
    """

    ACTIVE = "Active"
    QUESTIONABLE = "Questionable"
    DOUBTFUL = "Doubtful"
    OUT = "Out"
    IR = "IR"
    UNKNOWN = "Unknown"

    @property
    def severity(self) -> int:
        """Higher = worse availability. UNKNOWN means 'no signal', sorts as -1."""
        order = {
            Status.ACTIVE: 0,
            Status.QUESTIONABLE: 1,
            Status.DOUBTFUL: 2,
            Status.OUT: 3,
            Status.IR: 4,
            Status.UNKNOWN: -1,
        }
        return order[self]

    @property
    def is_problem(self) -> bool:
        """True for anything a manager needs to look at."""
        return self in (
            Status.QUESTIONABLE,
            Status.DOUBTFUL,
            Status.OUT,
            Status.IR,
        )


class SourceTier(str, Enum):
    OFFICIAL = "official"       # NFL.com, NFL inactives, team sites -> source of truth
    CROSS_CHECK = "cross_check"  # Sleeper, ESPN public, RSS -> early signal / disagreement


# --------------------------------------------------------------------------- #
# The normalized record every adapter returns
# --------------------------------------------------------------------------- #
class StatusRecord(BaseModel):
    """One player's status as reported by one source at one point in time."""

    player_name: str
    team: str = "FA"                       # canonical team code, "FA" if unknown
    status: Status = Status.UNKNOWN
    player_id_espn: Optional[str] = None
    player_id_sleeper: Optional[str] = None
    position: Optional[str] = None
    role_note: Optional[str] = None        # depth-chart / usage note when available
    raw: str = ""                          # original text, for auditing
    source: str = ""
    source_tier: SourceTier = SourceTier.CROSS_CHECK
    url: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("timestamp")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


class SourceResult(BaseModel):
    """Envelope returned by every adapter.

    A healthy run sets ``ok=True`` and fills ``records``. A broken scraper /
    dead API sets ``ok=False`` and ``error`` — the run continues and reconcile
    reports the source as *unavailable* rather than crashing.
    """

    source: str
    tier: SourceTier
    ok: bool = True
    records: list[StatusRecord] = Field(default_factory=list)
    error: Optional[str] = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def failed(cls, source: str, tier: SourceTier, error: str) -> "SourceResult":
        return cls(source=source, tier=tier, ok=False, error=error, records=[])


# --------------------------------------------------------------------------- #
# Config tree
# --------------------------------------------------------------------------- #
class EspnConfig(BaseModel):
    league_id: Optional[int] = None
    team_id: Optional[int] = None
    year: int = 2026
    # Secrets: prefer env (ESPN_SWID / ESPN_S2). YAML values are a fallback so
    # local experiments work, but config.yaml is git-ignored.
    swid: Optional[str] = None
    espn_s2: Optional[str] = None

    @property
    def configured(self) -> bool:
        return bool(self.league_id and self.team_id and self.swid and self.espn_s2)


class RosterEntry(BaseModel):
    """One player on my roster, with the lineup slot they occupy."""

    name: str
    team: str
    position: str                          # QB/RB/WR/TE/DST/K
    slot: str                              # QB/RB/WR/TE/FLEX/DST/K/BENCH/IR
    player_id_espn: Optional[str] = None   # fill in to skip fuzzy matching

    @property
    def is_starter(self) -> bool:
        return self.slot.upper() not in ("BENCH", "IR", "TAXI")


class NotifyConfig(BaseModel):
    provider: str = "stub"                 # stub | ntfy | pushover | discord | slack
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: Optional[str] = None
    # Everything credential-shaped is read from env, never YAML:
    #   NTFY_TOKEN, PUSHOVER_TOKEN, PUSHOVER_USER, DISCORD_WEBHOOK_URL, SLACK_WEBHOOK_URL
    priority: str = "default"


class SourceToggle(BaseModel):
    enabled: bool = True
    ttl_minutes: int = 180                 # cache TTL for this source
    min_interval_seconds: float = 2.0      # politeness: min gap between requests


class PolitenessConfig(BaseModel):
    user_agent: str = (
        "roster-health-monitor/0.1 (+https://github.com/herggie/ken; personal use)"
    )
    default_ttl_minutes: int = 180
    default_min_interval_seconds: float = 2.0
    cache_dir: str = ".cache"
    state_dir: str = ".state"


class Config(BaseModel):
    espn: EspnConfig = Field(default_factory=EspnConfig)
    roster: list[RosterEntry] = Field(default_factory=list)
    # Team sites to watch. Empty => derive from roster automatically.
    watch_teams: list[str] = Field(default_factory=list)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    politeness: PolitenessConfig = Field(default_factory=PolitenessConfig)
    sources: dict[str, SourceToggle] = Field(default_factory=dict)

    def source_toggle(self, name: str) -> SourceToggle:
        return self.sources.get(
            name,
            SourceToggle(
                ttl_minutes=self.politeness.default_ttl_minutes,
                min_interval_seconds=self.politeness.default_min_interval_seconds,
            ),
        )

    @property
    def starters(self) -> list[RosterEntry]:
        return [r for r in self.roster if r.is_starter]

    @property
    def bench(self) -> list[RosterEntry]:
        return [r for r in self.roster if not r.is_starter]

    def teams_to_watch(self) -> list[str]:
        if self.watch_teams:
            return sorted({t.upper() for t in self.watch_teams})
        return sorted({r.team.upper() for r in self.roster})


def coerce_extra(d: dict[str, Any]) -> dict[str, Any]:
    """Small helper: drop YAML keys we don't model so config stays forgiving."""
    return d
