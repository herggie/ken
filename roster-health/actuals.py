#!/usr/bin/env python3
"""This week's ACTUAL fantasy totals from the ESPN Fantasy API — the scoreboard
the app shows. Prints your team total, your opponent's total, and every
starter's actual points next to their projection so you can see who busted.

    python actuals.py --config config.yaml [--week N] [--push]

Runs where ESPN is reachable (GitHub Actions / local box). Uses espn-api's
box scores, which carry each player's real scored points for the week.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from models import Config
from monitor import load_config

log = logging.getLogger("roster-health.actuals")

_BENCH_SLOTS = {"BE", "Bench", "IR", "TAXI"}


def _num(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) else None


def build(config: Config, week: int | None = None) -> str:
    espn = config.espn
    swid = os.environ.get("ESPN_SWID") or espn.swid
    s2 = os.environ.get("ESPN_S2") or espn.espn_s2
    if not (espn.league_id and espn.team_id and swid and s2):
        return "ESPN not configured — need ESPN_SWID / ESPN_S2 + league & team id."

    try:
        from espn_api.football import League
    except ImportError:
        return "espn-api not installed."

    league = League(league_id=int(espn.league_id), year=int(espn.year), espn_s2=s2, swid=swid)
    wk = week or getattr(league, "current_week", None)
    try:
        box = league.box_scores(wk)
    except Exception as exc:  # noqa: BLE001
        return f"Could not load week {wk} box scores ({exc})."

    mine = None
    for m in box:
        if getattr(m.home_team, "team_id", None) == int(espn.team_id):
            mine = (m.home_lineup, m.home_score, m.away_team, m.away_score)
            break
        if getattr(m.away_team, "team_id", None) == int(espn.team_id):
            mine = (m.away_lineup, m.away_score, m.home_team, m.home_score)
            break
    if mine is None:
        return f"Couldn't find your team (id {espn.team_id}) in week {wk} box scores."

    lineup, my_score, opp_team, opp_score = mine
    opp_name = getattr(opp_team, "team_name", None) or "opponent"

    starters = [p for p in lineup if getattr(p, "slot_position", "") not in _BENCH_SLOTS]
    bench = [p for p in lineup if getattr(p, "slot_position", "") in _BENCH_SLOTS
             and getattr(p, "slot_position", "") != "IR"]

    out: list[str] = [f"WEEK {wk} — ACTUAL SCORES (from the ESPN Fantasy app)", ""]
    result = "W" if my_score > opp_score else ("T" if my_score == opp_score else "L")
    out.append(f"  YOUR TEAM: {my_score:.1f}    {opp_name}: {opp_score:.1f}    → {result} by {abs(my_score - opp_score):.1f}")
    out.append("")
    out.append("  STARTERS            actual   proj    diff")
    proj_sum = 0.0
    for p in sorted(starters, key=lambda x: -(_num(getattr(x, "points", None)) or 0.0)):
        name = getattr(p, "name", "?")
        slot = getattr(p, "slot_position", "")
        act = _num(getattr(p, "points", None)) or 0.0
        proj = _num(getattr(p, "projected_points", None))
        if proj is not None:
            proj_sum += proj
        diff = f"{act - proj:+.1f}" if proj is not None else "  —"
        proj_s = f"{proj:.1f}" if proj is not None else " —"
        out.append(f"  {slot:4} {name:20.20} {act:6.1f}  {proj_s:>5}  {diff:>6}")
    out.append("")
    out.append(f"  actual total ~{my_score:.1f}  vs  projected ~{proj_sum:.1f}  ({my_score - proj_sum:+.1f})")

    # Biggest busts (started, projected decently, scored well under)
    busts = []
    for p in starters:
        act = _num(getattr(p, "points", None)) or 0.0
        proj = _num(getattr(p, "projected_points", None))
        if proj is not None and proj - act >= 5.0:
            busts.append((proj - act, getattr(p, "name", "?"), act, proj))
    busts.sort(reverse=True)
    if busts:
        out.append("")
        out.append("  Biggest letdowns vs projection:")
        for miss, name, act, proj in busts[:4]:
            out.append(f"    {name}: {act:.1f} (proj {proj:.1f}, −{miss:.1f})")

    # Best bench points you left on the table
    bench_scores = sorted(
        ((_num(getattr(p, "points", None)) or 0.0, getattr(p, "name", "?"),
          getattr(p, "slot_position", "")) for p in bench),
        reverse=True,
    )
    top_bench = [b for b in bench_scores if b[0] > 0][:3]
    if top_bench:
        out.append("")
        out.append("  Points on your bench:")
        for pts, name, _slot in top_bench:
            out.append(f"    {name}: {pts:.1f}")

    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="This week's actual fantasy scores")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--week", type=int, default=None)
    parser.add_argument("--push", action="store_true", help="also send to your phone")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    config = load_config(args.config)
    text = build(config, args.week)
    print(text)

    if args.push:
        from notify import make_notifier
        try:
            make_notifier(config.notify, dry_run=False).send(
                "Week Scores", text, url="https://fantasy.espn.com/"
            )
            print(f"\n(pushed via {config.notify.provider})")
        except Exception as exc:  # noqa: BLE001
            print(f"\n(push failed via {config.notify.provider}: {exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
