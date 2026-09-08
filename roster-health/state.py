"""Last-run state: persist a snapshot of each player's status and diff against
it so we notify ONLY on change (or a fresh disagreement), never on every run.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from models import Config
from normalize import canonical_team, normalize_name
from reconcile import Digest, PlayerReport

log = logging.getLogger("roster-health.state")


def _key(rep: PlayerReport) -> str:
    return f"{normalize_name(rep.entry.name)}|{canonical_team(rep.entry.team)}"


def _snapshot(digest: Digest) -> dict[str, dict]:
    """Serializable per-player fingerprint we compare between runs."""
    snap: dict[str, dict] = {}
    for r in digest.reports:
        snap[_key(r)] = {
            "name": r.entry.name,
            "status": r.status.value,
            "official": r.official_status.value if r.official_status else None,
            "role_note": r.role_note,
            "disagreement": r.disagreement,
            "sources": {v.source: v.status.value for v in r.views},
        }
    return snap


class RunState:
    def __init__(self, config: Config):
        self.path = Path(config.politeness.state_dir) / "last_run.json"
        self.previous: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.previous = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("could not read last-run state (%s)", exc)

    def diff(self, digest: Digest) -> list[str]:
        """Return human-readable change lines vs the previous run."""
        current = _snapshot(digest)
        changes: list[str] = []
        for key, cur in current.items():
            prev = self.previous.get(key)
            name = cur.get("name") or key.split("|")[0].title()
            if prev is None:
                # first time we've seen the player — only shout if it's a problem
                if cur["status"] not in ("Active", "Unknown"):
                    changes.append(f"NEW: {name} is {cur['status']}")
                continue
            if cur["status"] != prev.get("status"):
                changes.append(
                    f"{name}: {prev.get('status')} → {cur['status']}"
                )
            if cur["role_note"] != prev.get("role_note") and cur["role_note"]:
                changes.append(f"{name}: role now '{cur['role_note']}'")
            if cur["disagreement"] and not prev.get("disagreement"):
                changes.append(f"{name}: sources now disagree")
        return changes

    def save(self, digest: Digest) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(_snapshot(digest), indent=2))
