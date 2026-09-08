"""Player ID crosswalk.

Joins my roster (names, teams, optional ESPN ids) to the ids used by the
status sources. Sleeper's player file carries ``espn_id`` for most players, so
Sleeper records are the bridge: from them we can index by ESPN id, Sleeper id,
and normalized name+team.

Resolution order for each roster entry:
  1. explicit ``player_id_espn`` from config  -> exact
  2. normalized name + team                    -> exact
  3. fuzzy name within the same team           -> fuzzy
  4. fuzzy name across all teams               -> fuzzy (last resort)

Resolved entries are persisted to ``.state/crosswalk.json`` and reused; the
crosswalk only rebuilds an entry that isn't resolved yet, or when
``--rebuild-crosswalk`` is passed. Unresolved players are LOGGED, never
silently dropped.
"""

from __future__ import annotations

import difflib
import json
import logging
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from models import Config, RosterEntry, SourceResult
from normalize import canonical_team, normalize_name

log = logging.getLogger("roster-health.crosswalk")


class ResolvedPlayer(BaseModel):
    roster_key: str
    name: str
    team: str
    position: str
    player_id_espn: Optional[str] = None
    player_id_sleeper: Optional[str] = None
    matched_by: str = "unresolved"     # exact_espn | exact_name | fuzzy | config | unresolved

    @property
    def resolved(self) -> bool:
        return self.matched_by != "unresolved"


def roster_key(entry: RosterEntry) -> str:
    return f"{normalize_name(entry.name)}|{canonical_team(entry.team)}"


class _RecordIndex:
    """Indexes source records (primarily Sleeper) for id lookup."""

    def __init__(self) -> None:
        self.by_espn: dict[str, dict] = {}
        self.by_name_team: dict[tuple[str, str], dict] = {}
        self.by_team_names: dict[str, list[tuple[str, dict]]] = {}
        self.all_names: list[tuple[str, dict]] = []

    def add(self, name: str, team: str, espn_id: str | None, sleeper_id: str | None) -> None:
        entry = {"name": name, "team": team, "espn": espn_id, "sleeper": sleeper_id}
        norm = normalize_name(name)
        if espn_id:
            self.by_espn.setdefault(espn_id, entry)
        self.by_name_team.setdefault((norm, team), entry)
        self.by_team_names.setdefault(team, []).append((norm, entry))
        self.all_names.append((norm, entry))


class Crosswalk:
    def __init__(self, config: Config):
        self.config = config
        self.path = Path(config.politeness.state_dir) / "crosswalk.json"
        self.entries: dict[str, ResolvedPlayer] = {}
        self._load()

    # -- persistence ----------------------------------------------------- #
    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self.entries = {k: ResolvedPlayer(**v) for k, v in data.items()}
                log.info("loaded crosswalk: %d resolved players", len(self.entries))
            except (json.JSONDecodeError, OSError, TypeError) as exc:
                log.warning("could not load crosswalk (%s); rebuilding", exc)
                self.entries = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({k: v.model_dump() for k, v in self.entries.items()}, indent=2)
        )

    # -- building -------------------------------------------------------- #
    @staticmethod
    def _index(source_results: list[SourceResult]) -> _RecordIndex:
        idx = _RecordIndex()
        for res in source_results:
            for rec in res.records:
                idx.add(rec.player_name, rec.team, rec.player_id_espn, rec.player_id_sleeper)
        return idx

    def build(
        self,
        roster: list[RosterEntry],
        source_results: list[SourceResult],
        *,
        force: bool = False,
    ) -> None:
        """Resolve any roster entry not already resolved (or all, if force)."""
        idx = self._index(source_results)
        unresolved: list[str] = []

        for entry in roster:
            key = roster_key(entry)
            if not force and key in self.entries and self.entries[key].resolved:
                continue
            resolved = self._resolve_one(entry, idx)
            self.entries[key] = resolved
            if not resolved.resolved:
                unresolved.append(f"{entry.name} ({entry.team})")

        if unresolved:
            log.warning(
                "UNRESOLVED players (kept, not dropped): %s", ", ".join(unresolved)
            )
        self.save()

    def _resolve_one(self, entry: RosterEntry, idx: _RecordIndex) -> ResolvedPlayer:
        key = roster_key(entry)
        team = canonical_team(entry.team)
        norm = normalize_name(entry.name)
        base = ResolvedPlayer(
            roster_key=key, name=entry.name, team=team, position=entry.position
        )

        # 1. explicit ESPN id from config
        if entry.player_id_espn and entry.player_id_espn in idx.by_espn:
            hit = idx.by_espn[entry.player_id_espn]
            return base.model_copy(update={
                "player_id_espn": entry.player_id_espn,
                "player_id_sleeper": hit["sleeper"],
                "matched_by": "config",
            })

        # 2. exact normalized name + team
        if (norm, team) in idx.by_name_team:
            hit = idx.by_name_team[(norm, team)]
            return base.model_copy(update={
                "player_id_espn": hit["espn"],
                "player_id_sleeper": hit["sleeper"],
                "matched_by": "exact_name",
            })

        # 3. fuzzy within same team
        same_team = idx.by_team_names.get(team, [])
        hit = self._fuzzy(norm, same_team)
        if hit:
            return base.model_copy(update={
                "player_id_espn": hit["espn"],
                "player_id_sleeper": hit["sleeper"],
                "matched_by": "fuzzy",
            })

        # 4. fuzzy across all teams (last resort)
        hit = self._fuzzy(norm, idx.all_names, cutoff=0.9)
        if hit:
            return base.model_copy(update={
                "player_id_espn": hit["espn"],
                "player_id_sleeper": hit["sleeper"],
                "matched_by": "fuzzy",
            })

        return base  # unresolved

    @staticmethod
    def _fuzzy(norm: str, candidates: list[tuple[str, dict]], cutoff: float = 0.82):
        if not candidates:
            return None
        names = [n for n, _ in candidates]
        match = difflib.get_close_matches(norm, names, n=1, cutoff=cutoff)
        if not match:
            return None
        for n, entry in candidates:
            if n == match[0]:
                return entry
        return None

    # -- lookup ---------------------------------------------------------- #
    def get(self, entry: RosterEntry) -> ResolvedPlayer:
        return self.entries.get(roster_key(entry)) or ResolvedPlayer(
            roster_key=roster_key(entry),
            name=entry.name,
            team=canonical_team(entry.team),
            position=entry.position,
        )
