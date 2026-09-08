#!/usr/bin/env python3
"""Weekly report — one comprehensive, easy-to-read readout for your team.

Three sections in one place:
  1. LINEUP (start/sit)  — who not to start, and the best replacement.
  2. BYE-WEEK CONFLICTS  — upcoming weeks where several starters are off.
  3. WAIVER IDEAS        — positions of need + candidates to look for.

    python report.py --config config.yaml
    SLEEPER_PLAYERS_FILE=fixtures/sleeper_players_sample.json python report.py

Reuses the same live Sleeper data and reconcile logic as the monitor. Bye
weeks come from the feed when present, else from config.bye_weeks. Waiver
availability is approximate without the ESPN league pull (see notes below).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from id_crosswalk import Crosswalk
from models import Config, Status
from monitor import load_config
from normalize import canonical_team, normalize_name
from reconcile import reconcile
from sources import sleeper
from sources.base import PoliteSession

log = logging.getLogger("roster-health.report")

RULE = "=" * 64


def _section(title: str) -> str:
    return f"\n{title}\n{'-' * len(title)}"


def _market(records, position: str, roster_names: set[str], limit: int = 5):
    """Candidate NFL starters at a position who aren't on the roster."""
    cands = [
        rec for rec in records
        if (rec.position or "").upper() == position.upper()
        and rec.status == Status.ACTIVE
        and normalize_name(rec.player_name) not in roster_names
        and rec.role_note and rec.role_note.endswith("starter")
    ]
    cands.sort(key=lambda rec: rec.player_name)
    return cands[:limit]


def build_report(config: Config) -> str:
    session = PoliteSession(config)
    res = sleeper.fetch(config, session)
    xw = Crosswalk(config)
    xw.build(config.roster, [res])
    digest = reconcile(config, [res], xw)

    records = res.records
    by_sleeper = {r.player_id_sleeper: r for r in records if r.player_id_sleeper}
    by_key = {(normalize_name(r.player_name), canonical_team(r.team)): r for r in records}

    def rec_for(entry):
        resolved = xw.get(entry)
        if resolved.player_id_sleeper and resolved.player_id_sleeper in by_sleeper:
            return by_sleeper[resolved.player_id_sleeper]
        return by_key.get((normalize_name(entry.name), canonical_team(entry.team)))

    out: list[str] = [RULE, f"  WEEKLY REPORT — {len(config.roster)}-player roster", f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"]
    if not res.ok:
        out.append(f"  ⚠ Sleeper data unavailable: {res.error}")
    out.append(RULE)

    # ---- 1. LINEUP -------------------------------------------------------
    out.append(_section("1) LINEUP — start / sit"))
    actions = digest.lineup_actions()
    if actions:
        for r in actions:
            out.append("   " + r.action_line())
    else:
        out.append("   ✅ No must-fix problems — no Out/Doubtful players in your starting lineup.")
    watch = [r for r in digest.problems() if not r.lineup_problem and r.entry.is_starter]
    if watch:
        out.append("   👀 Watch (Questionable starters):")
        for r in watch:
            repl = f" — cover: {r.recommended_replacement}" if r.recommended_replacement else ""
            out.append(f"      • {r.entry.name} ({r.entry.team} {r.entry.position}){repl}")

    # ---- 2. BYE-WEEK CONFLICTS ------------------------------------------
    out.append(_section("2) BYE-WEEK CONFLICTS"))
    byes: dict[int, list[str]] = {}
    unknown: list[str] = []
    for entry in config.starters:
        rec = rec_for(entry)
        wk = (rec.bye_week if rec and rec.bye_week else None) or config.bye_weeks.get(canonical_team(entry.team))
        if wk:
            byes.setdefault(int(wk), []).append(f"{entry.name} ({entry.team} {entry.position})")
        else:
            unknown.append(entry.name)
    conflicts = {wk: names for wk, names in byes.items() if len(names) >= config.bye_conflict_threshold}
    if conflicts:
        for wk in sorted(conflicts):
            names = conflicts[wk]
            out.append(f"   ⚠ Week {wk}: {len(names)} starters on bye — {', '.join(names)}")
        out.append("     (plan a waiver/trade so you're not short that week)")
    elif byes:
        out.append("   ✅ No weeks with multiple starters on bye.")
    if not byes:
        out.append("   (bye weeks not in the feed — set config.bye_weeks or enable the ESPN pull)")
    elif unknown:
        out.append(f"   (bye unknown for: {', '.join(unknown)})")

    # ---- 3. WAIVER IDEAS -------------------------------------------------
    out.append(_section("3) WAIVER IDEAS"))
    roster_names = {normalize_name(r.name) for r in config.roster}
    # A "need" = a starter who's Out/IR with no healthy bench cover at that position.
    needs: dict[str, str] = {}
    for r in digest.lineup_actions():
        if r.status in (Status.OUT, Status.IR) and not r.recommended_replacement:
            needs[r.entry.position.upper()] = r.entry.name
    if not needs:
        out.append("   ✅ No urgent holes — your Out starters have healthy bench cover.")
    else:
        for pos, who in needs.items():
            cands = _market(records, pos, roster_names)
            names = ", ".join(f"{c.player_name} ({c.team})" for c in cands) or "none found"
            out.append(f"   {pos} (need: {who} is out, no bench cover) → look at: {names}")
        out.append("     (candidates are NFL starters not on your roster — verify they're")
        out.append("      actually free in your league; true availability needs the ESPN pull)")

    # ---- 4. IR MANAGEMENT ------------------------------------------------
    out.append(_section("4) IR MANAGEMENT"))
    ir = digest.ir_moves()
    if not ir["returning"] and not ir["eligible"]:
        out.append("   ✅ Nothing to do — no IR returns and no IR-eligible players.")
    else:
        for r in ir["returning"]:
            out.append("   " + r.ir_line())
        for r in ir["eligible"]:
            out.append("   " + r.ir_line())
            pickups = _market(records, r.entry.position, roster_names)
            names = ", ".join(f"{c.player_name} ({c.team})" for c in pickups) or "none found"
            out.append(f"        fill the freed spot from waivers ({r.entry.position}): {names}")

    out.append("\n" + RULE)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Comprehensive weekly fantasy report")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--push", action="store_true",
                        help="also send the report to your phone via the configured notifier")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    config = load_config(args.config)
    report = build_report(config)
    print(report)

    if args.push:
        from notify import make_notifier
        notifier = make_notifier(config.notify, dry_run=False)
        try:
            notifier.send("📋 Weekly Fantasy Report", report, url="https://sleeper.com/")
            print("\n(report pushed via %s)" % config.notify.provider)
        except Exception as exc:  # noqa: BLE001
            print(f"\n(push failed via {config.notify.provider}: {exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
