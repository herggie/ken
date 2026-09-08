"""Sleeper adapter — cross-check tier.

Sleeper's public player file (`GET /v1/players/nfl`) is a large JSON object
keyed by Sleeper player id. It needs no auth, is stable, and — crucially —
carries ``espn_id`` for most players, which is what the id crosswalk uses to
bridge to the private ESPN league. It also carries structured injury status
and depth-chart info, so it's our best single feed to prove the pipeline.

The file is ~5 MB and changes slowly, so it is cached aggressively (default
24 h TTL, overridable in config). Set ``SLEEPER_PLAYERS_FILE`` to a local JSON
path to run fully offline (used by the shipped sample fixture and by CI smoke
tests).
"""

from __future__ import annotations

from models import Config, SourceResult, SourceTier, StatusRecord
from normalize import canonical_team, normalize_status
from sources.base import PoliteSession, fetch_guard

SOURCE = "sleeper"
TIER = SourceTier.CROSS_CHECK
PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"

# Positions we care about for fantasy. Everything else is dropped to keep the
# record set (and the crosswalk) focused.
_FANTASY_POS = {"QB", "RB", "WR", "TE", "K", "DEF"}


def _role_note(player: dict) -> str | None:
    pos = player.get("depth_chart_position")
    order = player.get("depth_chart_order")
    if pos and order:
        note = f"{pos}{order}"          # e.g. "RB1"
        if order == 1:
            note += " / starter"
        return note
    if pos:
        return str(pos)
    return None


def _is_fantasy(player: dict) -> bool:
    if player.get("position") in _FANTASY_POS:
        return True
    fps = player.get("fantasy_positions") or []
    return any(p in _FANTASY_POS for p in fps)


@fetch_guard(SOURCE, TIER)
def fetch(config: Config, session: PoliteSession) -> SourceResult:
    toggle = config.source_toggle(SOURCE)
    players: dict[str, dict] = session.get_json(
        PLAYERS_URL,
        cache_key="sleeper_players_nfl",
        # Players file changes slowly; hold it a day regardless of the generic
        # per-source TTL unless the config explicitly asks for tighter.
        ttl_minutes=max(toggle.ttl_minutes, 1440),
        min_interval=toggle.min_interval_seconds,
        timeout=45.0,
        local_file_env="SLEEPER_PLAYERS_FILE",
    )

    records: list[StatusRecord] = []
    for sleeper_id, p in players.items():
        if not isinstance(p, dict) or not _is_fantasy(p):
            continue
        team = p.get("team")
        if not team:                       # free agents: no roster relevance
            continue
        name = p.get("full_name") or " ".join(
            filter(None, [p.get("first_name"), p.get("last_name")])
        )
        if not name:
            name = p.get("last_name") or sleeper_id
        injury = p.get("injury_status")
        raw_bits = [str(injury or "Active")]
        if p.get("injury_body_part"):
            raw_bits.append(str(p["injury_body_part"]))
        if p.get("injury_notes"):
            raw_bits.append(str(p["injury_notes"]))
        espn_id = p.get("espn_id")
        bye = p.get("bye_week")
        records.append(
            StatusRecord(
                player_name=name,
                team=canonical_team(team),
                status=normalize_status(injury),
                player_id_espn=str(espn_id) if espn_id is not None else None,
                player_id_sleeper=str(sleeper_id),
                position=p.get("position"),
                role_note=_role_note(p),
                bye_week=int(bye) if isinstance(bye, (int, float)) else None,
                raw=" — ".join(raw_bits),
                source=SOURCE,
                source_tier=TIER,
                url="https://sleeper.com/",
            )
        )

    return SourceResult(source=SOURCE, tier=TIER, ok=True, records=records)
