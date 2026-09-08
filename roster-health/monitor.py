#!/usr/bin/env python3
"""Roster health monitor — entrypoint.

Pipeline:  config -> roster -> run each source (isolated) -> crosswalk ->
reconcile -> diff vs last run -> notify (only on change) -> persist state.

Usage:
    python monitor.py --config config.yaml
    python monitor.py --config config.example.yaml --dry-run
    python monitor.py --dry-run --only sleeper --rebuild-crosswalk -v

Every source runs behind ``fetch_guard`` so a single dead upstream degrades the
run ("source unavailable") instead of killing it. ``--dry-run`` prints the
digest and the notification that *would* be sent, sends nothing, and leaves
last-run state untouched (so the next real run still fires).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml

from id_crosswalk import Crosswalk
from models import Config, SourceResult, SourceTier
from notify import make_notifier
from reconcile import reconcile
from sources import (
    espn_public,
    espn_roster,
    news_rss,
    nfl_inactives,
    nfl_official,
    sleeper,
    team_sites,
)
from sources.base import PoliteSession
from state import RunState

log = logging.getLogger("roster-health")

# The whole extension surface: one row per source adapter.
SOURCES = {
    "sleeper": sleeper.fetch,
    "espn_public": espn_public.fetch,
    "nfl_official": nfl_official.fetch,
    "nfl_inactives": nfl_inactives.fetch,
    "team_sites": team_sites.fetch,
    "news_rss": news_rss.fetch,
}


# --------------------------------------------------------------------------- #
# Config loading (+ env secret overlay)
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> Config:
    if not path.exists():
        raise SystemExit(
            f"config not found: {path}\n"
            "Copy config.example.yaml to config.yaml and fill it in."
        )
    data = yaml.safe_load(path.read_text()) or {}
    config = Config(**data)

    # Secrets & overrides come from the environment (local .env / GH secrets).
    e = config.espn
    e.swid = os.environ.get("ESPN_SWID", e.swid)
    e.espn_s2 = os.environ.get("ESPN_S2", e.espn_s2)
    if os.environ.get("ESPN_LEAGUE_ID"):
        e.league_id = int(os.environ["ESPN_LEAGUE_ID"])
    if os.environ.get("ESPN_TEAM_ID"):
        e.team_id = int(os.environ["ESPN_TEAM_ID"])
    if os.environ.get("NTFY_TOPIC"):
        config.notify.ntfy_topic = os.environ["NTFY_TOPIC"]
    if os.environ.get("NOTIFY_PROVIDER"):
        config.notify.provider = os.environ["NOTIFY_PROVIDER"]
    if os.environ.get("NOTIFY_GITHUB_ISSUE"):
        config.notify.github_issue = int(os.environ["NOTIFY_GITHUB_ISSUE"])
    if os.environ.get("NOTIFY_GITHUB_REPO"):
        config.notify.github_repo = os.environ["NOTIFY_GITHUB_REPO"]
    return config


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
def run_sources(config: Config, only: set[str] | None) -> list[SourceResult]:
    session = PoliteSession(config)
    results: list[SourceResult] = []
    for name, fetch in SOURCES.items():
        if only and name not in only:
            continue
        if not config.source_toggle(name).enabled and not (only and name in only):
            log.info("source %s disabled in config; skipping", name)
            continue
        log.info("running source: %s", name)
        results.append(fetch(config, session))  # guarded: never raises
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fantasy roster health monitor")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--dry-run", action="store_true",
                        help="print digest + would-be notification, send nothing, don't persist state")
    parser.add_argument("--only", default="",
                        help="comma-separated source names to run (default: all enabled)")
    parser.add_argument("--rebuild-crosswalk", action="store_true",
                        help="re-resolve every roster player from scratch")
    parser.add_argument("--save-state", action="store_true",
                        help="persist last-run state even on --dry-run")
    parser.add_argument("--notify-now", action="store_true",
                        help="send the digest even if nothing changed (testing / on-demand)")
    parser.add_argument("--list-sources", action="store_true")
    parser.add_argument("--log-file", default=None,
                        help="append run + notification logs here (default: <state_dir>/monitor.log)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    # Console logging (quiet unless -v); a file handler is added once config is
    # loaded so the state dir is known. INFO-level records go to the file even
    # when the console is quiet, so there's always a durable trail to debug from.
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger("roster-health")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO if args.verbose else logging.WARNING)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if args.list_sources:
        for name, fetch in SOURCES.items():
            tier = "official" if name in ("nfl_official", "nfl_inactives", "team_sites") else "cross_check"
            print(f"{name:14s} [{tier}]")
        return 0

    config = load_config(args.config)

    # Now that we know the state dir, start appending to the log file.
    log_path = Path(args.log_file) if args.log_file else Path(config.politeness.state_dir) / "monitor.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        log.info("=== run start (dry_run=%s, notify_now=%s) ===", args.dry_run, args.notify_now)
    except OSError as exc:
        log.warning("could not open log file %s: %s", log_path, exc)

    only = {s.strip() for s in args.only.split(",") if s.strip()} or None

    # Roster: prefer live ESPN pull, fall back to config roster.
    live = espn_roster.fetch_roster(config)
    if live is not None:
        config.roster = live
    if not config.roster:
        raise SystemExit("empty roster: fill in 'roster:' in config, or configure ESPN.")

    source_results = run_sources(config, only)

    xw = Crosswalk(config)
    xw.build(config.roster, source_results, force=args.rebuild_crosswalk)

    digest = reconcile(config, source_results, xw)

    # --- output ---
    print(digest.render())
    print()

    state = RunState(config)
    changes = state.diff(digest)

    if changes:
        print("Changes vs last run:")
        for c in changes:
            print(f"  • {c}")
    elif args.notify_now:
        print("No changes vs last run — sending anyway (--notify-now).")
    else:
        print("No changes vs last run — nothing to notify.")

    if changes or args.notify_now:
        n_actions = len(digest.lineup_actions())
        if n_actions:
            title = f"🚨 LINEUP: {n_actions} starter(s) you should not start"
        else:
            title = f"Roster Health: {len(digest.problems())} issue(s)"
            if changes:
                title += f", {len(changes)} change(s)"
        body = digest.short()
        provider = "stub" if args.dry_run else config.notify.provider
        notifier = make_notifier(config.notify, dry_run=args.dry_run)
        try:
            notifier.send(title, body, url="https://sleeper.com/")
            log.info("notification sent via %s: %s", provider, title)
        except Exception as exc:  # noqa: BLE001 — never let notify failure crash the run
            log.error("notification FAILED via %s: %s", provider, exc)
            print(f"  notification failed via {provider}: {exc}")

    log.info(
        "run complete: %d issue(s), %d lineup-action(s), %d change(s), unavailable=%s",
        len(digest.problems()), len(digest.lineup_actions()), len(changes),
        ",".join(digest.unavailable_sources) or "none",
    )

    if not args.dry_run or args.save_state:
        state.save(digest)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
