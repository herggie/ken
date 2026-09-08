"""Reconciliation: join status records to my roster and build the digest.

Rules from the brief:

* Official tier (NFL.com, inactives, team sites) is the source of truth.
* Cross-check tier (Sleeper, ESPN public, RSS) is early warning + disagreement
  detection.
* Flag a player when the official designation is a problem, when a cross-check
  source disagrees with the official one, or when a depth-chart/role note
  looks like a demotion.
* For any flagged starter, surface healthy bench options at the same position.
* Emit a concise digest, problems first.

Whether to actually *notify* is decided against last-run state (see state.py);
this module only computes the current picture + within-run disagreements.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from id_crosswalk import Crosswalk
from models import Config, RosterEntry, SourceResult, SourceTier, Status, StatusRecord
from normalize import canonical_team, normalize_name


def _matches(rec: StatusRecord, entry: RosterEntry, xw: Crosswalk) -> bool:
    resolved = xw.get(entry)
    if resolved.player_id_espn and rec.player_id_espn == resolved.player_id_espn:
        return True
    if resolved.player_id_sleeper and rec.player_id_sleeper == resolved.player_id_sleeper:
        return True
    return (
        normalize_name(rec.player_name) == normalize_name(entry.name)
        and canonical_team(rec.team) == canonical_team(entry.team)
    )


def _worst(statuses: list[Status]) -> Status:
    real = [s for s in statuses if s != Status.UNKNOWN]
    if not real:
        return Status.UNKNOWN
    return max(real, key=lambda s: s.severity)


class SourceView(BaseModel):
    source: str
    tier: SourceTier
    status: Status
    role_note: Optional[str] = None
    url: Optional[str] = None
    raw: str = ""
    timestamp: datetime


class PlayerReport(BaseModel):
    entry: RosterEntry
    status: Status                      # effective status (official if present)
    official_status: Optional[Status] = None
    role_note: Optional[str] = None
    disagreement: bool = False
    disagreement_detail: Optional[str] = None
    views: list[SourceView] = Field(default_factory=list)
    sources_missing: list[str] = Field(default_factory=list)
    bench_options: list[str] = Field(default_factory=list)

    @property
    def has_alert(self) -> bool:
        return self.status.is_problem or self.disagreement

    @property
    def _sort_key(self) -> tuple:
        # alerts first; starters before bench; worse status first; name
        return (
            0 if self.has_alert else 1,
            0 if self.entry.is_starter else 1,
            -self.status.severity,
            self.entry.name,
        )

    def one_liner(self) -> str:
        flag = "🔴" if self.status.is_problem else ("🟡" if self.disagreement else "🟢")
        slot = self.entry.slot
        bits = [f"{flag} {self.entry.name} ({self.entry.team} {self.entry.position}, {slot}): {self.status.value}"]
        # where the status came from
        srcs = ", ".join(
            f"{v.source}={v.status.value}" for v in sorted(self.views, key=lambda v: v.tier.value)
        )
        if srcs:
            bits.append(f"[{srcs}]")
        if self.role_note:
            bits.append(f"— {self.role_note}")
        if self.disagreement and self.disagreement_detail:
            bits.append(f"⚠ {self.disagreement_detail}")
        if self.bench_options:
            bits.append(f"→ bench: {', '.join(self.bench_options)}")
        return " ".join(bits)


class Digest(BaseModel):
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reports: list[PlayerReport] = Field(default_factory=list)
    unavailable_sources: list[str] = Field(default_factory=list)

    def problems(self) -> list[PlayerReport]:
        return [r for r in self.reports if r.has_alert]

    def render(self) -> str:
        lines = [
            f"Roster Health — {self.generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        ]
        probs = self.problems()
        lines.append(
            f"{len(probs)} issue(s) across {len(self.reports)} rostered players."
        )
        if self.unavailable_sources:
            lines.append(f"Sources unavailable this run: {', '.join(self.unavailable_sources)}")
        lines.append("")
        for r in self.reports:
            lines.append("  " + r.one_liner())
        return "\n".join(lines)

    def short(self) -> str:
        """One-screen push body: problems only."""
        probs = self.problems()
        if not probs:
            return "All clear — no roster health issues."
        return "\n".join(r.one_liner() for r in probs)


def _bench_options(entry: RosterEntry, reports_by_key: dict, config: Config) -> list[str]:
    """Healthy bench players at the same position as a flagged starter."""
    out: list[str] = []
    for b in config.bench:
        if b.position != entry.position:
            continue
        # look up the bench player's own computed status if we have it
        rep = reports_by_key.get(f"{normalize_name(b.name)}|{canonical_team(b.team)}")
        status = rep.status if rep else Status.UNKNOWN
        if status in (Status.ACTIVE, Status.UNKNOWN):
            label = b.name if status == Status.ACTIVE else f"{b.name} (?)"
            out.append(label)
    return out


def reconcile(
    config: Config,
    source_results: list[SourceResult],
    xw: Crosswalk,
) -> Digest:
    unavailable = [r.source for r in source_results if not r.ok]

    # First pass: build a report per roster player.
    reports: list[PlayerReport] = []
    for entry in config.roster:
        views: list[SourceView] = []
        missing: list[str] = []
        for res in source_results:
            if not res.ok:
                # only note official-tier gaps per player; cross-check gaps are noise
                if res.tier == SourceTier.OFFICIAL:
                    missing.append(res.source)
                continue
            for rec in res.records:
                if _matches(rec, entry, xw):
                    views.append(
                        SourceView(
                            source=rec.source,
                            tier=rec.source_tier,
                            status=rec.status,
                            role_note=rec.role_note,
                            url=rec.url,
                            raw=rec.raw,
                            timestamp=rec.timestamp,
                        )
                    )
                    break  # one record per source per player

        official = [v for v in views if v.tier == SourceTier.OFFICIAL]
        cross = [v for v in views if v.tier == SourceTier.CROSS_CHECK]
        official_status = _worst([v.status for v in official]) if official else None
        cross_status = _worst([v.status for v in cross]) if cross else Status.UNKNOWN
        effective = official_status if official_status is not None else cross_status

        # disagreement detection
        disagreement = False
        detail: Optional[str] = None
        if official_status is not None:
            for v in cross:
                if v.status != Status.UNKNOWN and v.status != official_status:
                    disagreement = True
                    detail = (
                        f"{v.source} says {v.status.value} vs official "
                        f"{official_status.value}"
                    )
                    break
        else:
            distinct = {v.status for v in cross if v.status != Status.UNKNOWN}
            if len(distinct) > 1:
                disagreement = True
                detail = "cross-check sources disagree: " + ", ".join(
                    f"{v.source}={v.status.value}" for v in cross
                )

        role_note = next((v.role_note for v in views if v.role_note), None)

        reports.append(
            PlayerReport(
                entry=entry,
                status=effective,
                official_status=official_status,
                role_note=role_note,
                disagreement=disagreement,
                disagreement_detail=detail,
                views=views,
                sources_missing=missing,
            )
        )

    # Second pass: attach bench options to flagged starters.
    reports_by_key = {
        f"{normalize_name(r.entry.name)}|{canonical_team(r.entry.team)}": r
        for r in reports
    }
    for r in reports:
        if r.entry.is_starter and r.has_alert:
            r.bench_options = _bench_options(r.entry, reports_by_key, config)

    reports.sort(key=lambda r: r._sort_key)
    return Digest(reports=reports, unavailable_sources=unavailable)
