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
from collections import Counter
from pathlib import Path

from id_crosswalk import Crosswalk
from models import Config, RosterEntry, Status
from monitor import load_config
from normalize import canonical_team, normalize_name
from sources import espn_roster, sleeper
from sources.base import PoliteSession

log = logging.getLogger("roster-health.startsit")

_FLEX_ELIGIBLE = {"RB", "WR", "TE"}


def _eligible(bench: RosterEntry, slot: str) -> bool:
    slot = slot.upper()
    if slot == "FLEX":
        return bench.position.upper() in _FLEX_ELIGIBLE
    return bench.position.upper() == slot


_UNAVAILABLE = (Status.OUT, Status.IR, Status.DOUBTFUL)


def optimize(roster: list[RosterEntry], st: dict, config: Config):
    """Assign players to the lineup's slots to maximize total projection.

    Fills fixed position slots with the top healthy projected players, then FLEX
    from the remaining RB/WR/TE. Optimal for standard lineups (only FLEX has
    overlapping eligibility). Returns (chosen [(slot, player)], used id set).
    """
    startable = [
        r for r in roster
        if st[id(r)] not in _UNAVAILABLE and r.projection is not None
    ]
    slot_counts = Counter(s.slot.upper() for s in config.starters)
    chosen: list[tuple[str, RosterEntry]] = []
    used: set[int] = set()

    for slot, n in slot_counts.items():
        if slot == "FLEX":
            continue
        pool = sorted(
            (p for p in startable if p.position.upper() == slot and id(p) not in used),
            key=lambda p: -(p.projection or 0.0),
        )
        for p in pool[:n]:
            chosen.append((slot, p))
            used.add(id(p))

    for _ in range(slot_counts.get("FLEX", 0)):
        pool = sorted(
            (p for p in startable if p.position.upper() in _FLEX_ELIGIBLE and id(p) not in used),
            key=lambda p: -(p.projection or 0.0),
        )
        if pool:
            chosen.append(("FLEX", pool[0]))
            used.add(id(pool[0]))
    return chosen, used


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

    if have_proj:
        chosen, used = optimize(roster, st, config)
        opt_total = sum((proj(p) or 0.0) for _, p in chosen)
        # current lineup value: an Out/IR/Doubtful starter effectively scores 0
        cur_total = sum(
            (proj(r) or 0.0) if st[id(r)] not in _UNAVAILABLE else 0.0
            for r in starters
        )
        starter_ids = {id(s) for s in starters}
        start_new = [p for _, p in chosen if id(p) not in starter_ids]
        sit_old = [r for r in starters if id(r) not in used]

        out.append("OPTIMAL LINEUP (by projected points):")
        for slot, p in chosen:
            out.append(f"  {slot:5} {tag(p)}")
        delta = opt_total - cur_total
        out += [
            "",
            f"  Projected total: {opt_total:.0f} pts  "
            f"(your current lineup ~{cur_total:.0f} pts → "
            f"{'+' if delta >= 0 else ''}{delta:.0f})",
        ]
        if start_new or sit_old:
            out.append("  Make these changes:")
            for p in start_new:
                out.append(f"    ▶ START {tag(p)}")
            for r in sit_old:
                out.append(f"    ◀ SIT   {tag(r)}")
        else:
            out.append("  ✅ Your current lineup is already the highest-scoring one.")
    else:
        recs: list[str] = []
        for starter in starters:
            if st[id(starter)] in _UNAVAILABLE:
                pool = sorted(
                    (b for b in bench if _eligible(b, starter.slot) and st[id(b)] not in (Status.OUT, Status.IR)),
                    key=lambda b: -(proj(b) or 0.0),
                )
                if pool:
                    recs.append(f"🔁 SIT {tag(starter)} → START {tag(pool[0])}")
                else:
                    recs.append(f"⚠ SIT {tag(starter)} — no healthy bench at {starter.slot}; check waivers")
        out += recs or ["✅ No injured starters — your lineup looks fine on health."]
        out += [
            "",
            "(No ESPN projections yet, so this only flags injuries — it can't build "
            "a higher-scoring lineup from healthy players. Add ESPN cookies to unlock "
            "the full optimizer.)",
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
