#!/usr/bin/env python3
"""Start/Sit analyzer — weekly lineup decisions.

For each starting slot, compares your current starter against the bench players
eligible for that slot and recommends a change when one is warranted:

  * A starter who is Out/IR/Doubtful → sit him, start the best eligible bench.
  * A healthy bench player projected clearly higher than a healthy starter →
    "consider starting X over Y" (needs ESPN projections; with cookies set the
    roster pull provides them).

Without ESPN projections it can only act on injuries (start healthy over hurt);
it says so, since you can't rank two healthy players without a projection.

    python startsit.py --config config.yaml
    python startsit.py --config config.yaml --push      # send result to phone
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from id_crosswalk import Crosswalk
from models import Config, RosterEntry, Status
from monitor import load_config
from normalize import canonical_team, normalize_name
from sources import espn_roster, sleeper
from sources.base import PoliteSession

log = logging.getLogger("roster-health.startsit")

_FLEX_ELIGIBLE = {"RB", "WR", "TE"}
MARGIN = 1.5  # projected-point gap before suggesting a healthy-vs-healthy swap


def _eligible(bench: RosterEntry, slot: str) -> bool:
    slot = slot.upper()
    if slot == "FLEX":
        return bench.position.upper() in _FLEX_ELIGIBLE
    return bench.position.upper() == slot


def build(config: Config) -> str:
    # Roster (ESPN pull carries projections; else the config roster).
    live = espn_roster.fetch_roster(config)
    roster = live if live is not None else list(config.roster)
    have_proj = any(r.projection is not None for r in roster)

    session = PoliteSession(config)
    res = sleeper.fetch(config, session)
    by_key = {(normalize_name(r.player_name), canonical_team(r.team)): r for r in res.records}
    by_espn = {r.player_id_espn: r for r in res.records if r.player_id_espn}

    def status_of(entry: RosterEntry) -> Status:
        if entry.player_id_espn and entry.player_id_espn in by_espn:
            return by_espn[entry.player_id_espn].status
        rec = by_key.get((normalize_name(entry.name), canonical_team(entry.team)))
        return rec.status if rec else Status.UNKNOWN

    st = {id(r): status_of(r) for r in roster}
    starters = [r for r in roster if r.is_starter]
    bench = [r for r in roster if not r.is_starter]

    def proj(r: RosterEntry) -> float | None:
        return r.projection

    def tag(r: RosterEntry) -> str:
        s = st[id(r)]
        p = f" ~{proj(r):.0f}pts" if proj(r) is not None else ""
        flag = "" if s == Status.ACTIVE else f" [{s.value}]"
        return f"{r.name} ({r.team} {r.position}){flag}{p}"

    out: list[str] = ["Start/Sit Analyzer", ""]
    recs: list[str] = []

    for starter in starters:
        pool = [
            b for b in bench
            if _eligible(b, starter.slot) and st[id(b)] not in (Status.OUT, Status.IR)
        ]
        s_status = st[id(starter)]

        if s_status in (Status.OUT, Status.IR, Status.DOUBTFUL):
            # rank bench by projection (if any) else keep roster order
            pool.sort(key=lambda b: -(proj(b) or 0.0))
            if pool:
                recs.append(f"🔁 SIT {tag(starter)} → START {tag(pool[0])}")
            else:
                recs.append(f"⚠ SIT {tag(starter)} — no healthy bench at {starter.slot}; check waivers")
            continue

        # healthy starter: only comparable if we have projections on both sides
        if have_proj and proj(starter) is not None:
            better = [
                b for b in pool
                if st[id(b)] == Status.ACTIVE and proj(b) is not None
                and proj(b) > proj(starter) + MARGIN
            ]
            if better:
                best = max(better, key=lambda b: proj(b))
                recs.append(f"📈 CONSIDER START {tag(best)} over {tag(starter)}")

    if recs:
        out += recs
    else:
        out.append("✅ Lineup looks optimal — no changes recommended.")

    if not have_proj:
        out += [
            "",
            "(No ESPN projections yet, so this only flags injuries — it can't rank "
            "two healthy players. Add ESPN cookies to compare by projected points.)",
        ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly start/sit analyzer")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--push", action="store_true",
                        help="also send the result to your phone via the notifier")
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
                "📋 Start/Sit", text, url="https://sleeper.com/"
            )
            print(f"\n(pushed via {config.notify.provider})")
        except Exception as exc:  # noqa: BLE001
            print(f"\n(push failed via {config.notify.provider}: {exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
