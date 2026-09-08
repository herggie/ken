#!/usr/bin/env python3
"""Trade request validator.

Paste in a trade offer — who you'd GIVE and who you'd GET — and get back the
*facts*: each player's current injury status, position and depth-chart role
(from Sleeper), plus objective flags (are you acquiring a dinged player? does
the deal leave a starting slot short? does your roster size change?).

This reports facts, not a win/lose verdict — value is yours to judge.

    python trade.py --give "Saquon Barkley" --get "Malik Nabers, Xavier Worthy"
    # disambiguate same-named players with a team hint:
    python trade.py --give "Josh Allen (BUF)" --get "Mike Williams (NYJ)"

Offline demo (same firewall note as the monitor):
    SLEEPER_PLAYERS_FILE=fixtures/sleeper_players_sample.json \
      python trade.py --give "Christian McCaffrey" --get "Malik Nabers"
"""

from __future__ import annotations

import argparse
import difflib
import logging
import re
import sys
from pathlib import Path

from models import Config, Status, StatusRecord
from monitor import load_config
from normalize import canonical_team, normalize_name
from sources import sleeper
from sources.base import PoliteSession

log = logging.getLogger("roster-health.trade")

_TEAM_HINT = re.compile(r"\(([^)]+)\)\s*$")


def _parse_list(raw: str) -> list[tuple[str, str | None]]:
    """'A, B (TEAM), C' -> [(A, None), (B, TEAM), (C, None)]."""
    out: list[tuple[str, str | None]] = []
    for chunk in raw.split(","):
        name = chunk.strip()
        if not name:
            continue
        team = None
        m = _TEAM_HINT.search(name)
        if m:
            team = canonical_team(m.group(1))
            name = _TEAM_HINT.sub("", name).strip()
        out.append((name, team))
    return out


def _build_index(records: list[StatusRecord]):
    by_key: dict[tuple[str, str], StatusRecord] = {}
    by_name: dict[str, list[StatusRecord]] = {}
    for r in records:
        norm = normalize_name(r.player_name)
        by_key[(norm, canonical_team(r.team))] = r
        by_name.setdefault(norm, []).append(r)
    return by_key, by_name


def _resolve(name: str, team: str | None, by_key, by_name) -> StatusRecord | None:
    norm = normalize_name(name)
    if team and (norm, team) in by_key:
        return by_key[(norm, team)]
    if norm in by_name:
        hits = by_name[norm]
        if team:
            for r in hits:
                if canonical_team(r.team) == team:
                    return r
        return hits[0]  # exact name; if ambiguous, first (team hint disambiguates)
    # fuzzy fallback
    match = difflib.get_close_matches(norm, list(by_name), n=1, cutoff=0.85)
    if match:
        return by_name[match[0]][0]
    return None


def _fact(r: StatusRecord) -> str:
    flag = "🔴" if r.status.is_problem else "🟢"
    bits = [f"{flag} {r.player_name} ({r.team} {r.position or '?'}) — {r.status.value}"]
    if r.role_note:
        bits.append(f"— {r.role_note}")
    return " ".join(bits)


def _pos_counts(records: list[StatusRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in records:
        pos = (r.position or "?").upper()
        counts[pos] = counts.get(pos, 0) + 1
    return counts


def validate(give_recs, get_recs) -> list[str]:
    lines: list[str] = []

    # position delta
    g, h = _pos_counts(give_recs), _pos_counts(get_recs)
    positions = sorted(set(g) | set(h))
    deltas = []
    for p in positions:
        d = h.get(p, 0) - g.get(p, 0)
        if d:
            deltas.append(f"{'+' if d > 0 else ''}{d} {p}")
    lines.append("• Position change: " + (", ".join(deltas) if deltas else "none (like-for-like)"))

    # roster size
    size = len(get_recs) - len(give_recs)
    if size == 0:
        lines.append("• Roster size: unchanged (even swap)")
    elif size > 0:
        lines.append(f"• Roster size: +{size} — you'll need to DROP {size} player(s) to fit them")
    else:
        lines.append(f"• Roster size: {size} — you'll have {-size} open slot(s) to fill via waivers")

    # health of incoming
    dinged_in = [r for r in get_recs if r.status.is_problem]
    if dinged_in:
        lines.append(
            "• ⚠ Acquiring dinged player(s): "
            + ", ".join(f"{r.player_name} ({r.status.value})" for r in dinged_in)
        )
    else:
        lines.append("• Incoming players: all currently Active")

    dinged_out = [r for r in give_recs if r.status.is_problem]
    if dinged_out:
        lines.append(
            "• Note: you're sending away dinged player(s): "
            + ", ".join(f"{r.player_name} ({r.status.value})" for r in dinged_out)
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fantasy trade fact-checker")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--give", required=True, help="players you'd give, comma-separated")
    parser.add_argument("--get", required=True, help="players you'd receive, comma-separated")
    parser.add_argument("--push", action="store_true",
                        help="also send the result to your phone via the configured notifier")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    config = load_config(args.config)
    session = PoliteSession(config)
    result = sleeper.fetch(config, session)
    if not result.ok:
        print(f"Could not load player data from Sleeper: {result.error}")
        return 1
    by_key, by_name = _build_index(result.records)

    give_req = _parse_list(args.give)
    get_req = _parse_list(args.get)

    give_recs, get_recs, missing = [], [], []
    for name, team in give_req:
        r = _resolve(name, team, by_key, by_name)
        (give_recs if r else missing).append(r or name)
    for name, team in get_req:
        r = _resolve(name, team, by_key, by_name)
        (get_recs if r else missing).append(r or name)

    out: list[str] = ["Trade Validator — facts only, not a verdict", "", "YOU GIVE:"]
    out += ["  " + _fact(r) for r in give_recs]
    out += ["", "YOU GET:"]
    out += ["  " + _fact(r) for r in get_recs]
    if missing:
        out += ["", "⚠ Could not find (check spelling, or add a team hint like 'Name (BUF)'):"]
        out += [f"  - {m}" for m in missing]
    out += ["", "FACTS & FLAGS:"]
    out += validate(give_recs, get_recs)
    out += [
        "",
        "(Status/role are a live snapshot from Sleeper. This tool states facts; "
        "the value call is yours. Projection-based value needs the ESPN pull.)",
    ]
    text = "\n".join(out)
    print(text)

    if args.push:
        from notify import make_notifier
        try:
            make_notifier(config.notify, dry_run=False).send(
                "🔁 Trade check", text, url="https://sleeper.com/"
            )
            print(f"\n(pushed via {config.notify.provider})")
        except Exception as exc:  # noqa: BLE001
            print(f"\n(push failed via {config.notify.provider}: {exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
