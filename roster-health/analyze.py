#!/usr/bin/env python3
"""Comprehensive league-true roster analysis.

Pulls your roster, bench, and the free-agent pool from the ESPN Fantasy API,
scores everything with YOUR league's rulebook (via sources/espn_fantasy), layers
on Sleeper injury status, and prints:

  1. Your starting lineup (projection + health) and the OPTIMAL lineup.
  2. Your bench (projection + health).
  3. The best available free agents at each position — the pool to draw from.
  4. Concrete suggestions: start/sit moves and waiver upgrades where a healthy
     free agent clearly out-projects a bench player you're holding.

    python analyze.py --config config.yaml [--push]

Runs where ESPN is reachable (GitHub Actions / local box); fails soft to the
config roster when ESPN isn't configured.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from models import Config, RosterEntry, Status
from monitor import load_config
from normalize import canonical_team, normalize_name
from sources import espn_roster, sleeper
from sources.base import PoliteSession
from startsit import optimize

log = logging.getLogger("roster-health.analyze")

_FLEX = {"RB", "WR", "TE"}
_UNAVAIL = (Status.OUT, Status.IR, Status.DOUBTFUL)
# Positions worth surfacing a waiver pool for, and how many to show.
_POOL_POS = ["QB", "RB", "WR", "TE", "K", "DST"]
# A free agent must beat a bench player by at least this many projected points
# before we call it a real upgrade (avoids churn on noise).
_UPGRADE_MARGIN = 2.0


def _proj(x) -> float | None:
    return getattr(x, "projection", None)


def _pstr(p: float | None) -> str:
    return f"{p:.1f}" if p is not None else "—"


def _status_lookup(config: Config, roster: list[RosterEntry]):
    """Map each roster entry -> Sleeper status (health), best-effort."""
    session = PoliteSession(config)
    res = sleeper.fetch(config, session)
    by_espn = {r.player_id_espn: r for r in res.records if r.player_id_espn}
    by_key = {(normalize_name(r.player_name), canonical_team(r.team)): r for r in res.records}

    def status_of(e: RosterEntry) -> Status:
        if e.player_id_espn and e.player_id_espn in by_espn:
            return by_espn[e.player_id_espn].status
        rec = by_key.get((normalize_name(e.name), canonical_team(e.team)))
        return rec.status if rec else Status.UNKNOWN

    return {id(e): status_of(e) for e in roster}


def build(config: Config) -> str:
    live = espn_roster.fetch_roster(config)
    roster = live if live is not None else list(config.roster)
    have_proj = any(_proj(r) is not None for r in roster)
    free_agents = espn_roster.fetch_free_agents(config, size=200) or []
    st = _status_lookup(config, roster)

    def flag(s: Status) -> str:
        return "" if s in (Status.ACTIVE, Status.UNKNOWN) else f" [{s.value}]"

    def line(e: RosterEntry) -> str:
        return f"{e.name} ({e.team} {e.position}){flag(st[id(e)])}  ~{_pstr(_proj(e))}"

    starters = [r for r in roster if r.is_starter]
    bench = [r for r in roster if not r.is_starter and r.slot.upper() != "IR"]
    ir = [r for r in roster if r.slot.upper() == "IR"]

    out: list[str] = ["LEAGUE-TRUE ROSTER ANALYSIS", "(projections computed from your league's scoring rulebook)", ""]

    # ---- 1. Lineup + optimal ----
    out.append("1) STARTING LINEUP")
    for r in sorted(starters, key=lambda x: -(_proj(x) or 0)):
        out.append(f"   {r.slot:5} {line(r)}")
    cur_total = sum((_proj(r) or 0.0) for r in starters if st[id(r)] not in _UNAVAIL)
    out.append(f"   → current projected total: ~{cur_total:.1f} pts")
    out.append("")

    if have_proj:
        chosen, used = optimize(roster, st, config)
        opt_total = sum((_proj(p) or 0.0) for _, p in chosen)
        starter_ids = {id(s) for s in starters}
        start_new = [p for _, p in chosen if id(p) not in starter_ids]
        sit_old = [r for r in starters if id(r) not in used]
        out.append("2) OPTIMAL LINEUP (highest-scoring legal lineup)")
        for slot, p in chosen:
            out.append(f"   {slot:5} {line(p)}")
        delta = opt_total - cur_total
        out.append(f"   → optimal total: ~{opt_total:.1f} pts ({'+' if delta >= 0 else ''}{delta:.1f} vs current)")
        if start_new or sit_old:
            for p in start_new:
                out.append(f"     ▶ START {line(p)}")
            for r in sit_old:
                out.append(f"     ◀ SIT   {line(r)}")
        else:
            out.append("     ✅ your current lineup is already optimal on projection")
        out.append("")

    # ---- 3. Bench ----
    out.append("3) BENCH")
    for r in sorted(bench, key=lambda x: -(_proj(x) or 0)):
        out.append(f"   {line(r)}")
    if ir:
        out.append("   IR:")
        for r in ir:
            out.append(f"     {line(r)}")
    out.append("")

    # ---- 4. Free-agent pool by position ----
    roster_names = {normalize_name(r.name) for r in roster}
    healthy_fa = [
        f for f in free_agents
        if normalize_name(f.name) not in roster_names and f.status not in (Status.OUT, Status.IR)
    ]
    out.append("4) BEST AVAILABLE (free agents in your league, by league-true projection)")
    fa_by_pos: dict[str, list] = {}
    for pos in _POOL_POS:
        pool = sorted(
            (f for f in healthy_fa if f.position.upper() == pos),
            key=lambda f: -(_proj(f) or 0.0),
        )
        fa_by_pos[pos] = pool
        top = pool[:5]
        if top:
            out.append(f"   {pos}:")
            for f in top:
                out.append(f"     {f.name} ({f.team}){flag(f.status)}  ~{_pstr(_proj(f))}")
    out.append("")

    # ---- 5. Suggestions: waiver upgrades over bench ----
    out.append("5) SUGGESTIONS")
    suggestions: list[str] = []
    if have_proj:
        # For each startable position, does the best FA clearly beat my weakest
        # bench player who could occupy that role?
        for pos in ("RB", "WR", "TE", "QB", "K", "DST"):
            fas = fa_by_pos.get(pos) or []
            if not fas:
                continue
            best_fa = fas[0]
            fap = _proj(best_fa) or 0.0
            bench_here = [b for b in bench if b.position.upper() == pos]
            if not bench_here:
                continue
            weakest = min(bench_here, key=lambda b: (_proj(b) or 0.0))
            wp = _proj(weakest) or 0.0
            if fap - wp >= _UPGRADE_MARGIN:
                suggestions.append(
                    f"   ⬆ {pos}: consider ADD {best_fa.name} ({best_fa.team}, ~{_pstr(fap)}) "
                    f"→ DROP {weakest.name} ({weakest.team}, ~{_pstr(wp)})  (+{fap - wp:.1f})"
                )
        # Injured starters with no healthy same-position bench cover -> waiver need
        for s in starters:
            if st[id(s)] in _UNAVAIL:
                cover = [b for b in bench if b.position.upper() == s.position.upper()
                         and st[id(b)] not in _UNAVAIL]
                if not cover:
                    fas = fa_by_pos.get(s.position.upper()) or []
                    add = f" → add {fas[0].name} ({fas[0].team}, ~{_pstr(_proj(fas[0]))})" if fas else ""
                    suggestions.append(f"   ⚠ {s.position}: {s.name} is {st[id(s)].value} with no healthy bench cover{add}")
    if not have_proj:
        suggestions.append("   (no ESPN projections available — set ESPN cookies to unlock full analysis)")
    out += suggestions or ["   ✅ No clear upgrades — your roster is projection-efficient this week."]

    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="League-true roster analysis")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--push", action="store_true", help="also send to your phone")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    config = load_config(args.config)
    text = build(config)
    print(text)

    if args.push:
        from notify import make_notifier
        try:
            make_notifier(config.notify, dry_run=False).send(
                "Roster Analysis", text, url="https://fantasy.espn.com/"
            )
            print(f"\n(pushed via {config.notify.provider})")
        except Exception as exc:  # noqa: BLE001
            print(f"\n(push failed via {config.notify.provider}: {exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
