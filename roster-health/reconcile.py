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
    def lineup_problem(self) -> bool:
        """A STARTER who probably/definitely won't play — a lineup you must fix."""
        return self.entry.is_starter and self.status in (
            Status.DOUBTFUL, Status.OUT, Status.IR,
        )

    @property
    def recommended_replacement(self) -> Optional[str]:
        """Best healthy (Active) bench option — the top-ranked one with no tag."""
        for b in self.bench_options:
            if "(" not in b:
                return b
        return None

    def action_line(self) -> str:
        verb = "DO NOT START" if self.status in (Status.OUT, Status.IR) else "RISKY START"
        if self.recommended_replacement:
            fix = f" → start {self.recommended_replacement} instead"
        elif self.bench_options:
            fix = f" → only bench option is {self.bench_options[0]}"
        else:
            fix = " → NO bench option at this position — pick up a free agent"
        return (
            f"🚨 {verb}: {self.entry.name} ({self.entry.team} {self.entry.position}, "
            f"{self.entry.slot}) is {self.status.value}{fix}"
        )

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

    def lineup_actions(self) -> list[PlayerReport]:
        """Starters you must not leave in your lineup (Out / IR / Doubtful)."""
        return [r for r in self.reports if r.lineup_problem]

    def render(self) -> str:
        lines = [
            f"Roster Health — {self.generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        ]
        probs = self.problems()
        actions = self.lineup_actions()
        lines.append(
            f"{len(probs)} issue(s) across {len(self.reports)} rostered players."
        )
        if self.unavailable_sources:
            lines.append(f"Sources unavailable this run: {', '.join(self.unavailable_sources)}")
        lines.append("")
        if actions:
            lines.append("LINEUP ACTIONS — fix before kickoff:")
            for r in actions:
                lines.append("  " + r.action_line())
            lines.append("")
        for r in self.reports:
            lines.append("  " + r.one_liner())
        return "\n".join(lines)

    def short(self) -> str:
        """One-screen push body: lineup actions first, then the rest of the watch list."""
        actions = self.lineup_actions()
        probs = self.problems()
        if not actions and not probs:
            return "All clear — no roster health issues."
        out: list[str] = []
        if actions:
            out.append("LINEUP ACTIONS — fix before kickoff:")
            out.extend(r.action_line() for r in actions)
        watch = [r for r in probs if not r.lineup_problem]
        if watch:
            if out:
                out.append("")
            out.append("Watch:")
            out.extend(r.one_liner() for r in watch)
        return "\n".join(out)


_FLEX_ELIGIBLE = {"RB", "WR", "TE"}

_STATUS_TAG = {
    Status.QUESTIONABLE: "Q",
    Status.DOUBTFUL: "D",
    Status.UNKNOWN: "?",
}


def _eligible_positions(entry: RosterEntry) -> set[str]:
    """Which bench positions can fill this starter's slot."""
    if entry.slot.upper() == "FLEX":
        return _FLEX_ELIGIBLE
    return {entry.position.upper()}


def _bench_label(entry: RosterEntry, status: Status) -> str:
    """'Name', 'Name (Q)', 'Name ~14pts', 'Name (Q) ~9pts' — parens only for dinged."""
    parts = [entry.name]
    tag = _STATUS_TAG.get(status)
    if tag:
        parts.append(f"({tag})")
    if entry.projection is not None:
        parts.append(f"~{entry.projection:.0f}pts")
    return " ".join(parts)


def _bench_options(entry: RosterEntry, reports_by_key: dict, config: Config) -> list[str]:
    """Best bench replacements for a flagged starter, ranked best-first.

    Ranking:
      1. healthy (Active) before dinged;
      2. higher ESPN projection first (when the ESPN pull provided one);
      3. NFL starters before backups (fallback signal when no projection);
      4. otherwise roster order.
    Out/IR bench players are dropped (can't replace an Out starter with one).
    """
    elig = _eligible_positions(entry)
    scored: list[tuple] = []
    for i, b in enumerate(config.bench):
        if b.position.upper() not in elig:
            continue
        rep = reports_by_key.get(f"{normalize_name(b.name)}|{canonical_team(b.team)}")
        status = rep.status if rep else Status.UNKNOWN
        role = rep.role_note if rep else None
        if status in (Status.OUT, Status.IR):
            continue  # not a viable replacement
        healthy = status == Status.ACTIVE
        # higher projection sorts first; unprojected rosters fall back to role
        proj_key = -b.projection if b.projection is not None else 0.0
        nfl_starter = bool(role and role.endswith("starter"))
        scored.append((0 if healthy else 1, proj_key, 0 if nfl_starter else 1, i, b, status))

    scored.sort(key=lambda s: (s[0], s[1], s[2], s[3]))
    return [_bench_label(b, status) for *_, b, status in scored]


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
