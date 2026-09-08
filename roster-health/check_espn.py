#!/usr/bin/env python3
"""ESPN cookie checker.

Run this after setting your ESPN cookies to confirm they work — before relying
on the scheduled runs. It attempts the private-league roster pull and the
free-agent pull and reports a clear PASS/FAIL with the reason.

    # locally (cookies in env):
    set ESPN_SWID={...}      # PowerShell: $env:ESPN_SWID="{...}"
    set ESPN_S2=...
    python check_espn.py --config config.yaml

Exit code is 0 on success, 1 on failure (handy for CI). Nothing here prints or
stores the cookie values.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from monitor import load_config
from sources import espn_roster


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify ESPN cookies work")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--push", action="store_true", help="send the result to your phone")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    config = load_config(args.config)
    swid = os.environ.get("ESPN_SWID") or config.espn.swid
    espn_s2 = os.environ.get("ESPN_S2") or config.espn.espn_s2

    out = [
        "ESPN cookie check",
        f"  league_id: {config.espn.league_id or '(missing)'}",
        f"  team_id:   {config.espn.team_id or '(missing)'}",
        f"  SWID:      {'set (' + str(len(swid)) + ' chars)' if swid else 'MISSING'}",
        f"  espn_s2:   {'set (' + str(len(espn_s2)) + ' chars)' if espn_s2 else 'MISSING'}",
    ]
    ok = False

    if not (config.espn.league_id and config.espn.team_id and swid and espn_s2):
        out.append("\n❌ Not fully configured. Set ESPN_LEAGUE_ID / ESPN_TEAM_ID "
                   "(or config) and ESPN_SWID / ESPN_S2 (env), then re-run.")
    else:
        roster = espn_roster.fetch_roster(config)
        if roster is None:
            out.append("\n❌ ESPN auth FAILED — cookies rejected or league/team not found. "
                       "Re-copy SWID (with braces) and the full espn_s2 and try again.")
        else:
            fas = espn_roster.fetch_free_agents(config)
            starters = [r for r in roster if r.is_starter]
            have_proj = any(r.projection is not None for r in roster)
            out.append(f"\n✅ ESPN auth OK — {len(roster)} rostered players"
                       + (f", {len(fas)} free agents." if fas is not None else "."))
            out.append(f"   starters: {len(starters)} | projections: {'yes' if have_proj else 'no'}")
            out.append("   Optimizer, auto-roster, and real free agents are now live.")
            ok = True

    text = "\n".join(out)
    print(text)
    if args.push:
        from notify import make_notifier
        try:
            make_notifier(config.notify, dry_run=False).send("🔑 ESPN check", text)
        except Exception as exc:  # noqa: BLE001
            print(f"\n(push failed via {config.notify.provider}: {exc})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
