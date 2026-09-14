"""Offline pytest suite for ``sources/espn_fantasy.py``.

Everything here runs fully offline. ESPN is NOT reachable from this
environment, so we NEVER make a network call:

* Pure parsing/computation functions (``parse_scoring``, ``ScoringRules``,
  ``_projected_stat_line``, ``_espn_applied_total``, ``_build_projection``,
  ``_iter_roster_players``) are exercised directly against hand-built dict
  fixtures shaped like real ESPN Fantasy API JSON.
* The one network-orchestrating function we cover
  (``projections_by_espn_id``) is tested by monkeypatching
  ``espn_fantasy._api_get`` (and ``espn_fantasy.configured``) with fakes that
  return fixture dicts. ``_api_get``/``get_scoring``/``_current_week``/
  ``build_compare``/``main`` are never called against a live API.

Hand-computed expected point totals are spelled out in comments so a reader
can verify the arithmetic without re-deriving the league rulebook.

Runnable two ways::

    python -m pytest tests/test_espn_fantasy.py -v
    python tests/test_espn_fantasy.py

The second form uses a tiny built-in runner (see the ``__main__`` block) so the
file still works where pytest is not installed.
"""

from __future__ import annotations

import os
import sys

# Make ``from sources import espn_fantasy`` importable no matter whether pytest
# is launched from ``roster-health/`` or from the repo root. The roster-health
# directory is the parent of this file's ``tests/`` directory.
_ROSTER_HEALTH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROSTER_HEALTH_DIR not in sys.path:
    sys.path.insert(0, _ROSTER_HEALTH_DIR)

from models import Config, EspnConfig  # noqa: E402
from sources import espn_fantasy  # noqa: E402
from sources.espn_fantasy import (  # noqa: E402
    PlayerProjection,
    ScoringRules,
    _build_projection,
    _espn_applied_total,
    _iter_roster_players,
    _projected_stat_line,
    parse_scoring,
)

# statSourceId constants, mirrored from the module for fixture clarity.
ACTUAL = 0
PROJECTED = 1

# The year all fixtures use; matches EspnConfig's default.
YEAR = 2026


# --------------------------------------------------------------------------- #
# Fixture builders (plain functions so the manual runner can use them too)
# --------------------------------------------------------------------------- #
def scoring_items_ppr() -> dict:
    """A minimal PPR-ish ``mSettings`` response.

    default point values used across the suite:
        3  pass yds   0.04
        4  pass TD    4.0
        24 rush yds   0.1
        25 rush TD    6.0   (override: QB(posId 1) => 4.0)
        42 rec yds    0.1
        43 rec TD     6.0
        53 receptions 1.0   (full PPR)
    """
    return {
        "settings": {
            "scoringSettings": {
                "scoringItems": [
                    {"statId": 3, "points": 0.04},
                    {"statId": 4, "points": 4.0},
                    {"statId": 24, "points": 0.1},
                    {
                        "statId": 25,
                        "points": 6.0,
                        # position ids arrive as STRINGS from ESPN and must be
                        # parsed to ints. QB (1) scores a rushing TD as 4.
                        "pointsOverrides": {"1": 4.0},
                    },
                    {"statId": 42, "points": 0.1},
                    {"statId": 43, "points": 6.0},
                    {"statId": 53, "points": 1.0},
                ]
            }
        }
    }


def rules_ppr() -> ScoringRules:
    """The parsed ScoringRules for the PPR fixture above."""
    return parse_scoring(scoring_items_ppr())


def wr_player_with_projection() -> dict:
    """A WR (defaultPositionId 3) carrying an ACTUAL entry (ignored), a weekly
    PROJECTED entry for week 5, and a season PROJECTED split (fallback).

    Weekly projected stat line: 6 receptions, 88 rec yds, 1 rec TD.
    appliedTotal (ESPN's own number) = 19.5.
    """
    return {
        "id": 3139477,
        "fullName": "Test Wideout",
        "defaultPositionId": 3,
        "stats": [
            # Real/actual production — must be ignored by projection extraction.
            {
                "statSourceId": ACTUAL,
                "scoringPeriodId": 5,
                "appliedTotal": 4.2,
                "stats": {"53": 3, "42": 42, "43": 0},
            },
            # Season projection split — only used as a fallback when no weekly.
            {
                "statSourceId": PROJECTED,
                "scoringPeriodId": 0,
                "seasonId": YEAR,
                "appliedTotal": 210.0,
                "stats": {"53": 90, "42": 1200, "43": 8},
            },
            # The one we want: week-specific projection.
            {
                "statSourceId": PROJECTED,
                "scoringPeriodId": 5,
                "appliedTotal": 19.5,
                "stats": {"53": "6", "42": "88", "43": "1"},
            },
        ],
    }


def dst_player_actual_only() -> dict:
    """A D/ST (defaultPositionId 16) with ONLY an actual entry — the classic
    'ESPN hasn't posted a projection yet' case. Nothing to score.
    """
    return {
        "id": -16001,
        "fullName": "Test D/ST",
        "defaultPositionId": 16,
        "stats": [
            {
                "statSourceId": ACTUAL,
                "scoringPeriodId": 5,
                "appliedTotal": 7.0,
                "stats": {"99": 3, "95": 1},
            }
        ],
    }


# --------------------------------------------------------------------------- #
# 1. parse_scoring
# --------------------------------------------------------------------------- #
def test_parse_scoring_builds_default_map():
    """Normal items become the ``default`` statId -> points map."""
    rules = rules_ppr()
    assert rules.default[3] == 0.04
    assert rules.default[4] == 4.0
    assert rules.default[24] == 0.1
    assert rules.default[42] == 0.1
    assert rules.default[43] == 6.0
    assert rules.default[53] == 1.0
    # All default values are floats even when the fixture gave an int.
    assert all(isinstance(v, float) for v in rules.default.values())


def test_parse_scoring_parses_overrides_with_int_position_keys():
    """``pointsOverrides`` keys arrive stringified and must become ints."""
    rules = rules_ppr()
    assert rules.overrides[25] == {1: 4.0}
    # The override key is a real int, not the original "1" string.
    assert 1 in rules.overrides[25]
    assert "1" not in rules.overrides[25]


def test_parse_scoring_skips_item_missing_stat_id():
    """An item without a statId is skipped, not fatal."""
    payload = {
        "settings": {
            "scoringSettings": {
                "scoringItems": [
                    {"points": 5.0},  # no statId -> KeyError -> skipped
                    {"statId": 53, "points": 1.0},
                ]
            }
        }
    }
    rules = parse_scoring(payload)
    assert rules.default == {53: 1.0}


def test_parse_scoring_skips_item_with_non_numeric_stat_id():
    """A non-integer statId (e.g. 'abc' or None) is skipped."""
    payload = {
        "settings": {
            "scoringSettings": {
                "scoringItems": [
                    {"statId": "abc", "points": 5.0},  # ValueError -> skipped
                    {"statId": None, "points": 5.0},  # TypeError -> skipped
                    {"statId": 42, "points": 0.1},
                ]
            }
        }
    }
    rules = parse_scoring(payload)
    assert rules.default == {42: 0.1}


def test_parse_scoring_skips_non_numeric_points():
    """A valid statId with non-numeric points contributes no default value."""
    payload = {
        "settings": {
            "scoringSettings": {
                "scoringItems": [
                    {"statId": 4, "points": "lots"},  # points ignored
                    {"statId": 53, "points": 1.0},
                ]
            }
        }
    }
    rules = parse_scoring(payload)
    assert 4 not in rules.default
    assert rules.default == {53: 1.0}


def test_parse_scoring_skips_bad_override_values():
    """Un-parseable override entries are dropped; good ones survive."""
    payload = {
        "settings": {
            "scoringSettings": {
                "scoringItems": [
                    {
                        "statId": 25,
                        "points": 6.0,
                        "pointsOverrides": {"1": 4.0, "bad": "x", "2": "nope"},
                    }
                ]
            }
        }
    }
    rules = parse_scoring(payload)
    # Only the "1" -> 4.0 pair is parseable.
    assert rules.overrides[25] == {1: 4.0}


def test_parse_scoring_empty_items_is_not_usable():
    """An empty scoringItems list yields an unusable ScoringRules."""
    rules = parse_scoring({"settings": {"scoringSettings": {"scoringItems": []}}})
    assert rules.default == {}
    assert rules.overrides == {}
    assert rules.usable is False


def test_parse_scoring_missing_settings_is_not_usable():
    """A totally empty payload yields an unusable ScoringRules, no exception."""
    assert parse_scoring({}).usable is False
    assert parse_scoring({"settings": None}).usable is False
    assert parse_scoring({"settings": {}}).usable is False


def test_scoring_rules_usable_flag():
    """``usable`` is True as soon as there is any default or override."""
    assert ScoringRules().usable is False
    assert ScoringRules(default={53: 1.0}).usable is True
    assert ScoringRules(overrides={25: {1: 4.0}}).usable is True


# --------------------------------------------------------------------------- #
# 2. ScoringRules.points_for
# --------------------------------------------------------------------------- #
def test_points_for_returns_default_when_no_override_map():
    """A statId with no override map returns its default at any position."""
    rules = rules_ppr()
    assert rules.points_for(42, None) == 0.1  # rec yds, no position
    assert rules.points_for(42, 2) == 0.1  # rec yds, RB position, no override


def test_points_for_returns_override_when_position_matches():
    """The position-specific override wins when the position id matches."""
    rules = rules_ppr()
    # rush TD (25) for a QB (posId 1) is overridden to 4.0.
    assert rules.points_for(25, 1) == 4.0


def test_points_for_returns_default_for_non_overridden_position():
    """A position with no override entry falls back to the default value."""
    rules = rules_ppr()
    # rush TD (25) for an RB (posId 2) has no override -> default 6.0.
    assert rules.points_for(25, 2) == 6.0
    # And with no position at all, also the default.
    assert rules.points_for(25, None) == 6.0


def test_points_for_unknown_stat_id_is_zero():
    """An unknown statId scores nothing, with or without a position."""
    rules = rules_ppr()
    assert rules.points_for(9999, None) == 0.0
    assert rules.points_for(9999, 1) == 0.0


# --------------------------------------------------------------------------- #
# 3. ScoringRules.score
# --------------------------------------------------------------------------- #
def test_score_full_ppr_receiving_line():
    """A full-PPR receiving line dot-products to a known total.

    6 receptions x 1.0 = 6.0
    88 rec yds  x 0.1 = 8.8
    1  rec TD   x 6.0 = 6.0
    -------------------------
                        20.8
    """
    rules = rules_ppr()
    stat_line = {53: 6, 42: 88, 43: 1}
    assert rules.score(stat_line, position_id=3) == 20.8


def test_score_honors_position_override_qb_vs_rb():
    """The same rushing-TD line scores differently for QB vs RB.

    stat line: 50 rush yds (0.1) + 1 rush TD.
      QB (posId 1): rush TD override 4.0 -> 50*0.1 + 1*4.0 = 5.0 + 4.0 = 9.0
      RB (posId 2): default 6.0          -> 50*0.1 + 1*6.0 = 5.0 + 6.0 = 11.0
    """
    rules = rules_ppr()
    stat_line = {24: 50, 25: 1}
    assert rules.score(stat_line, position_id=1) == 9.0  # QB
    assert rules.score(stat_line, position_id=2) == 11.0  # RB


def test_score_ignores_unknown_stats():
    """Unknown statIds contribute 0 and don't blow up the sum."""
    rules = rules_ppr()
    # 53 receptions counts (1.0 each); 9999 is unknown (0.0).
    assert rules.score({53: 4, 9999: 100}, position_id=3) == 4.0


def test_score_empty_stat_line_is_zero():
    """An empty stat line scores exactly 0.0."""
    assert rules_ppr().score({}, position_id=3) == 0.0


# --------------------------------------------------------------------------- #
# 4. _projected_stat_line
# --------------------------------------------------------------------------- #
def test_projected_stat_line_prefers_weekly_entry():
    """The week-specific projected entry wins over the season split."""
    player = wr_player_with_projection()
    line = _projected_stat_line(player, week=5, year=YEAR)
    # Weekly stats {"53":"6","42":"88","43":"1"} coerced to int keys/float vals.
    assert line == {53: 6.0, 42: 88.0, 43: 1.0}


def test_projected_stat_line_ignores_actual_entries():
    """A player whose only entry is actual (statSourceId 0) yields {}."""
    player = {
        "id": 1,
        "defaultPositionId": 3,
        "stats": [
            {
                "statSourceId": ACTUAL,
                "scoringPeriodId": 5,
                "stats": {"53": 3, "42": 40},
            }
        ],
    }
    assert _projected_stat_line(player, week=5, year=YEAR) == {}


def test_projected_stat_line_falls_back_to_season_split():
    """With no weekly projection, the season projection split is used."""
    player = {
        "id": 1,
        "defaultPositionId": 3,
        "stats": [
            {
                "statSourceId": PROJECTED,
                "scoringPeriodId": 0,
                "seasonId": YEAR,
                "stats": {"53": "90", "42": "1200"},
            }
        ],
    }
    # Week 5 has no weekly projection -> fall back to the season split.
    assert _projected_stat_line(player, week=5, year=YEAR) == {53: 90.0, 42: 1200.0}


def test_projected_stat_line_returns_empty_when_nothing_projected():
    """No stats at all -> {} (and no exception)."""
    assert _projected_stat_line({"id": 1}, week=5, year=YEAR) == {}
    assert _projected_stat_line({"id": 1, "stats": []}, week=5, year=YEAR) == {}
    assert _projected_stat_line({"id": 1, "stats": None}, week=5, year=YEAR) == {}


def test_projected_stat_line_coerces_and_skips_bad_pairs():
    """String keys/values are coerced; un-coercible pairs are dropped."""
    player = {
        "id": 1,
        "defaultPositionId": 3,
        "stats": [
            {
                "statSourceId": PROJECTED,
                "scoringPeriodId": 5,
                # "bad" key -> int() ValueError; "nope" value -> float() error.
                "stats": {"53": "6", "bad": "3", "42": "nope"},
            }
        ],
    }
    assert _projected_stat_line(player, week=5, year=YEAR) == {53: 6.0}


def test_projected_stat_line_skips_projected_entry_with_empty_stats():
    """A projected weekly entry with an empty stats map is not chosen; the
    season fallback (if any) or {} is returned instead."""
    player = {
        "id": 1,
        "defaultPositionId": 3,
        "stats": [
            {"statSourceId": PROJECTED, "scoringPeriodId": 5, "stats": {}},
        ],
    }
    assert _projected_stat_line(player, week=5, year=YEAR) == {}


def test_projected_stat_line_skips_stat_split_type_2():
    """statSplitTypeId==2 is an undocumented split ESPN's clients ignore; a
    projected weekly entry tagged that way is not used as the stat line."""
    player = {
        "id": 1,
        "defaultPositionId": 3,
        "stats": [
            {"statSourceId": PROJECTED, "scoringPeriodId": 5,
             "statSplitTypeId": 2, "stats": {"53": "99"}},  # skipped
        ],
    }
    assert _projected_stat_line(player, week=5, year=YEAR) == {}


# --------------------------------------------------------------------------- #
# 5. _espn_applied_total
# --------------------------------------------------------------------------- #
def test_espn_applied_total_returns_weekly_projected_total():
    """Returns the appliedTotal from the projected weekly entry."""
    player = wr_player_with_projection()
    assert _espn_applied_total(player, week=5) == 19.5


def test_espn_applied_total_none_when_no_projected_week():
    """No projected entry for the requested week -> None."""
    player = wr_player_with_projection()
    assert _espn_applied_total(player, week=9) is None


def test_espn_applied_total_none_when_applied_total_absent():
    """A projected weekly entry lacking appliedTotal -> None."""
    player = {
        "id": 1,
        "stats": [
            {"statSourceId": PROJECTED, "scoringPeriodId": 5, "stats": {"53": 6}},
        ],
    }
    assert _espn_applied_total(player, week=5) is None


def test_espn_applied_total_ignores_actual_total():
    """The actual entry's appliedTotal is never returned as the projection."""
    player = {
        "id": 1,
        "stats": [
            {"statSourceId": ACTUAL, "scoringPeriodId": 5, "appliedTotal": 4.2,
             "stats": {"53": 3}},
        ],
    }
    assert _espn_applied_total(player, week=5) is None


def test_espn_applied_total_skips_stat_split_type_2():
    """A statSplitTypeId==2 projected entry is ignored for the applied total."""
    player = {
        "id": 1,
        "stats": [
            {"statSourceId": PROJECTED, "scoringPeriodId": 5,
             "statSplitTypeId": 2, "appliedTotal": 99.0, "stats": {"53": 6}},
        ],
    }
    assert _espn_applied_total(player, week=5) is None


# --------------------------------------------------------------------------- #
# 6. _build_projection
# --------------------------------------------------------------------------- #
def test_build_projection_end_to_end_wr():
    """A WR with a weekly projection produces correct computed & espn points.

    computed (PPR): 6*1.0 + 88*0.1 + 1*6.0 = 6.0 + 8.8 + 6.0 = 20.8
    espn_points   : appliedTotal = 19.5
    """
    proj = _build_projection(wr_player_with_projection(), rules_ppr(), week=5, year=YEAR)
    assert proj.player_id == "3139477"
    assert proj.name == "Test Wideout"
    assert proj.position == "WR"
    # position_id is the *lineup-slot* id (WR=4), converted from defaultPositionId
    # (WR=3) so pointsOverrides (keyed by slot id) resolve correctly.
    assert proj.position_id == 4
    assert proj.week == 5
    assert proj.stat_line == {53: 6.0, 42: 88.0, 43: 1.0}
    assert proj.computed_points == 20.8
    assert proj.espn_points == 19.5
    assert proj.has_projection is True
    # points prefers our computed value.
    assert proj.points == 20.8


def test_build_projection_dst_no_projection_is_honest_none():
    """A D/ST with only actual data reports 'no projection' -- never a fake 0."""
    proj = _build_projection(dst_player_actual_only(), rules_ppr(), week=5, year=YEAR)
    assert proj.position == "DST"
    assert proj.position_id == 16
    assert proj.stat_line == {}
    assert proj.computed_points is None
    assert proj.espn_points is None
    assert proj.has_projection is False
    # Crucially, we do not fabricate a 0 -- points is None.
    assert proj.points is None


def test_build_projection_uses_name_fallback_and_unknown_position():
    """Missing fullName falls back to name; unknown positionId -> '?'."""
    player = {
        "id": 7,
        "name": "Fallback Name",  # no fullName
        # no defaultPositionId
        "stats": [
            {"statSourceId": PROJECTED, "scoringPeriodId": 5,
             "appliedTotal": 3.0, "stats": {"53": 3}},
        ],
    }
    proj = _build_projection(player, rules_ppr(), week=5, year=YEAR)
    assert proj.name == "Fallback Name"
    assert proj.position == "?"
    assert proj.position_id is None
    # 3 receptions * 1.0 = 3.0 computed; espn = 3.0
    assert proj.computed_points == 3.0
    assert proj.espn_points == 3.0


def test_build_projection_computed_none_when_rules_not_usable():
    """With an unusable rulebook there is no computed value, but ESPN's total
    (appliedTotal) is still reported."""
    proj = _build_projection(wr_player_with_projection(), ScoringRules(), week=5, year=YEAR)
    assert proj.computed_points is None
    assert proj.espn_points == 19.5
    assert proj.has_projection is True
    assert proj.points == 19.5  # falls back to ESPN's number


def test_build_projection_position_id_is_lineup_slot():
    """position_id carries the lineup-SLOT id derived from defaultPositionId.

    _DEFPOS_TO_SLOT: 1 QB->0, 2 RB->2, 3 WR->4, 4 TE->6, 5 K->17, 16 D/ST->16.
    The human-readable ``position`` label still reflects defaultPositionId.
    """
    rules = rules_ppr()
    cases = {  # defaultPositionId: (position label, expected slot id)
        1: ("QB", 0),
        2: ("RB", 2),
        3: ("WR", 4),
        4: ("TE", 6),
        5: ("K", 17),
        16: ("DST", 16),
    }
    for default_pos_id, (label, slot) in cases.items():
        player = {
            "id": default_pos_id,
            "defaultPositionId": default_pos_id,
            "stats": [{"statSourceId": PROJECTED, "scoringPeriodId": 5,
                       "appliedTotal": 1.0, "stats": {"53": 1}}],
        }
        proj = _build_projection(player, rules, week=5, year=YEAR)
        assert proj.position == label
        assert proj.position_id == slot


def test_build_projection_override_resolves_via_slot_id():
    """A slot-keyed override is honored end-to-end because _build_projection
    scores with the mapped lineup-slot id, not the raw defaultPositionId.

    Rulebook: rush yds (24)=0.1; rush TD (25)=6.0 default, but SLOT 0 (QB)=4.0.
    Projected line: 50 rush yds + 1 rush TD.
      QB (defaultPositionId 1 -> slot 0): 50*0.1 + 1*4.0 = 5.0 + 4.0 = 9.0
      RB (defaultPositionId 2 -> slot 2): 50*0.1 + 1*6.0 = 5.0 + 6.0 = 11.0
    """
    rules = ScoringRules(default={24: 0.1, 25: 6.0}, overrides={25: {0: 4.0}})
    entry = {"statSourceId": PROJECTED, "scoringPeriodId": 5,
             "stats": {"24": "50", "25": "1"}}
    qb = {"id": 1, "defaultPositionId": 1, "stats": [entry]}
    rb = {"id": 2, "defaultPositionId": 2, "stats": [entry]}
    assert _build_projection(qb, rules, week=5, year=YEAR).computed_points == 9.0
    assert _build_projection(rb, rules, week=5, year=YEAR).computed_points == 11.0


# --------------------------------------------------------------------------- #
# 7. PlayerProjection.points precedence
# --------------------------------------------------------------------------- #
def test_points_prefers_computed_over_espn():
    p = PlayerProjection(
        player_id="1", name="n", position="WR", week=5,
        computed_points=10.0, espn_points=9.0,
    )
    assert p.points == 10.0
    assert p.has_projection is True


def test_points_falls_back_to_espn_when_computed_none():
    p = PlayerProjection(
        player_id="1", name="n", position="WR", week=5,
        computed_points=None, espn_points=9.0,
    )
    assert p.points == 9.0
    assert p.has_projection is True


def test_points_none_when_both_none():
    p = PlayerProjection(
        player_id="1", name="n", position="WR", week=5,
        computed_points=None, espn_points=None,
    )
    assert p.points is None
    assert p.has_projection is False


def test_points_prefers_computed_zero_over_espn():
    """A legitimate computed 0.0 still wins over ESPN's number (0.0 is not None)."""
    p = PlayerProjection(
        player_id="1", name="n", position="WR", week=5,
        computed_points=0.0, espn_points=9.0,
    )
    assert p.points == 0.0


# --------------------------------------------------------------------------- #
# 8. _iter_roster_players
# --------------------------------------------------------------------------- #
def _roster_fixture(scoring_period=5) -> dict:
    """An mRoster response with two teams, each with rostered players."""
    return {
        "scoringPeriodId": scoring_period,
        "teams": [
            {
                "id": 1,
                "roster": {
                    "entries": [
                        {"playerPoolEntry": {"player": {"id": 101, "fullName": "Alpha"}}},
                        {"playerPoolEntry": {"player": {"id": 102, "fullName": "Bravo"}}},
                    ]
                },
            },
            {
                "id": 2,
                "roster": {
                    "entries": [
                        {"playerPoolEntry": {"player": {"id": 201, "fullName": "Charlie"}}},
                    ]
                },
            },
        ],
    }


def test_iter_roster_players_yields_across_teams():
    """Every player across every team's roster is yielded."""
    players = list(_iter_roster_players(_roster_fixture()))
    ids = sorted(p["id"] for p in players)
    assert ids == [101, 102, 201]


def test_iter_roster_players_tolerates_missing_pieces():
    """Missing/empty teams, roster, entries or playerPoolEntry never raise."""
    assert list(_iter_roster_players({})) == []
    assert list(_iter_roster_players({"teams": None})) == []
    assert list(_iter_roster_players({"teams": []})) == []
    assert list(_iter_roster_players({"teams": [{}]})) == []
    assert list(_iter_roster_players({"teams": [{"roster": None}]})) == []
    assert list(_iter_roster_players({"teams": [{"roster": {"entries": None}}]})) == []
    assert list(_iter_roster_players({"teams": [{"roster": {"entries": [{}]}}]})) == []
    assert list(_iter_roster_players(
        {"teams": [{"roster": {"entries": [{"playerPoolEntry": {}}]}}]}
    )) == []
    # A well-formed entry mixed with broken ones still yields the good player.
    mixed = {
        "teams": [
            {"roster": {"entries": [
                {},  # no playerPoolEntry
                {"playerPoolEntry": {"player": None}},  # not a dict
                {"playerPoolEntry": {"player": {"id": 5, "fullName": "Good"}}},
            ]}}
        ]
    }
    good = list(_iter_roster_players(mixed))
    assert [p["id"] for p in good] == [5]


# --------------------------------------------------------------------------- #
# 9. parse_scoring + score integration
# --------------------------------------------------------------------------- #
def test_parse_scoring_then_score_integration():
    """Build rules from a fixture and score a fixture player's stat line.

    QB line: 300 pass yds (0.04) + 3 pass TD (4.0) + 20 rush yds (0.1)
             + 1 rush TD (QB override 4.0)
      = 12.0 + 12.0 + 2.0 + 4.0 = 30.0
    """
    rules = parse_scoring(scoring_items_ppr())
    qb_line = {3: 300, 4: 3, 24: 20, 25: 1}
    assert rules.score(qb_line, position_id=1) == 30.0


# --------------------------------------------------------------------------- #
# 10. projections_by_espn_id (monkeypatched, no network)
# --------------------------------------------------------------------------- #
def _make_config() -> Config:
    """A Config whose only relevant fields are the ESPN league id + year."""
    return Config(espn=EspnConfig(league_id=123456, year=YEAR))


def _projections_roster_fixture() -> dict:
    """mRoster with two teams; each rostered player carries a weekly projection.

    Player 101 (WR): 5 rec (1.0) + 70 rec yds (0.1) = 5.0 + 7.0 = 12.0
    Player 202 (RB): 80 rush yds (0.1) + 1 rush TD (default 6.0) = 8.0 + 6.0 = 14.0
    """
    return {
        "scoringPeriodId": 5,
        "teams": [
            {
                "id": 1,
                "roster": {"entries": [
                    {"playerPoolEntry": {"player": {
                        "id": 101, "fullName": "Rostered WR", "defaultPositionId": 3,
                        "stats": [{
                            "statSourceId": PROJECTED, "scoringPeriodId": 5,
                            "appliedTotal": 11.7,
                            "stats": {"53": "5", "42": "70", "43": "0"},
                        }],
                    }}},
                ]},
            },
            {
                "id": 2,
                "roster": {"entries": [
                    {"playerPoolEntry": {"player": {
                        "id": 202, "fullName": "Rostered RB", "defaultPositionId": 2,
                        "stats": [{
                            "statSourceId": PROJECTED, "scoringPeriodId": 5,
                            "appliedTotal": 13.9,
                            "stats": {"24": "80", "25": "1"},
                        }],
                    }}},
                ]},
            },
        ],
    }


def _free_agent_fixture() -> dict:
    """kona_player_info with one free agent.

    Player 303 (WR): 8 rec (1.0) + 100 rec yds (0.1) + 1 rec TD (6.0)
      = 8.0 + 10.0 + 6.0 = 24.0
    """
    return {
        "players": [
            {"player": {
                "id": 303, "fullName": "Free Agent WR", "defaultPositionId": 3,
                "stats": [{
                    "statSourceId": PROJECTED, "scoringPeriodId": 5,
                    "appliedTotal": 22.5,
                    "stats": {"53": "8", "42": "100", "43": "1"},
                }],
            }},
        ]
    }


def _fake_api_get_factory():
    """Return a fake ``_api_get`` that dispatches on the requested view."""
    def fake_api_get(config, views, filter_header=None):
        if "mSettings" in views:
            return scoring_items_ppr()
        if "mRoster" in views:
            return _projections_roster_fixture()
        if "kona_player_info" in views:
            return _free_agent_fixture()
        raise AssertionError(f"unexpected views requested offline: {views}")
    return fake_api_get


def test_projections_by_espn_id_happy_path(monkeypatch):
    """With fake data, returns a dict keyed by ESPN id with correct projections.

    Includes rostered players (101, 202) and the free agent (303).
    """
    espn_fantasy._CACHE.clear()
    monkeypatch.setattr(espn_fantasy, "configured", lambda cfg: True)
    monkeypatch.setattr(espn_fantasy, "_api_get", _fake_api_get_factory())

    out = espn_fantasy.projections_by_espn_id(_make_config(), week=None)

    assert out is not None
    assert set(out) == {"101", "202", "303"}

    # WR 101: 5*1.0 + 70*0.1 + 0 = 12.0
    assert out["101"].position == "WR"
    assert out["101"].computed_points == 12.0
    assert out["101"].espn_points == 11.7
    assert out["101"].points == 12.0

    # RB 202: 80*0.1 + 1*6.0 = 14.0
    assert out["202"].position == "RB"
    assert out["202"].computed_points == 14.0
    assert out["202"].espn_points == 13.9

    # Free agent WR 303: 8*1.0 + 100*0.1 + 1*6.0 = 24.0
    assert out["303"].position == "WR"
    assert out["303"].computed_points == 24.0
    assert out["303"].espn_points == 22.5


def test_projections_by_espn_id_excludes_free_agents_when_asked(monkeypatch):
    """include_free_agents=False leaves the FA pool out entirely."""
    espn_fantasy._CACHE.clear()
    monkeypatch.setattr(espn_fantasy, "configured", lambda cfg: True)
    monkeypatch.setattr(espn_fantasy, "_api_get", _fake_api_get_factory())

    out = espn_fantasy.projections_by_espn_id(
        _make_config(), week=5, include_free_agents=False
    )
    assert out is not None
    assert set(out) == {"101", "202"}
    assert "303" not in out


def test_projections_by_espn_id_none_when_not_configured(monkeypatch):
    """When credentials aren't configured, the function returns None."""
    espn_fantasy._CACHE.clear()
    monkeypatch.setattr(espn_fantasy, "configured", lambda cfg: False)
    # _api_get must never be called in this path; make it explode if it is.
    def boom(*a, **k):
        raise AssertionError("_api_get must not be called when unconfigured")
    monkeypatch.setattr(espn_fantasy, "_api_get", boom)

    assert espn_fantasy.projections_by_espn_id(_make_config()) is None


def test_projections_by_espn_id_uses_current_week_from_roster(monkeypatch):
    """week=None resolves the scoring period from the mRoster payload itself
    (scoringPeriodId), so no mStatus network call is needed."""
    espn_fantasy._CACHE.clear()
    monkeypatch.setattr(espn_fantasy, "configured", lambda cfg: True)
    monkeypatch.setattr(espn_fantasy, "_api_get", _fake_api_get_factory())

    out = espn_fantasy.projections_by_espn_id(_make_config(), week=None)
    assert out is not None
    # Every projection carries the week pulled from the roster fixture (5).
    assert all(p.week == 5 for p in out.values())


def test_projections_by_espn_id_is_cached(monkeypatch):
    """A second call with identical args reuses the memoized result and does
    not re-hit the (fake) API."""
    espn_fantasy._CACHE.clear()
    monkeypatch.setattr(espn_fantasy, "configured", lambda cfg: True)

    calls = {"n": 0}
    base = _fake_api_get_factory()

    def counting_api_get(config, views, filter_header=None):
        calls["n"] += 1
        return base(config, views, filter_header)

    monkeypatch.setattr(espn_fantasy, "_api_get", counting_api_get)

    cfg = _make_config()
    first = espn_fantasy.projections_by_espn_id(cfg, week=5)
    calls_after_first = calls["n"]
    second = espn_fantasy.projections_by_espn_id(cfg, week=5)

    assert first is second  # same cached object
    assert calls["n"] == calls_after_first  # no additional API calls


# --------------------------------------------------------------------------- #
# Manual runner: works when pytest is not installed.
# --------------------------------------------------------------------------- #
class _ManualMonkeypatch:
    """Tiny stand-in for pytest's monkeypatch fixture (setattr + undo)."""

    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self):
        for target, name, old in reversed(self._undo):
            setattr(target, name, old)
        self._undo.clear()


def _run_manually() -> int:
    import inspect
    import traceback

    tests = sorted(
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and inspect.isfunction(obj)
    )
    passed = failed = 0
    for name, fn in tests:
        # Always start each test with a clean module cache.
        espn_fantasy._CACHE.clear()
        params = inspect.signature(fn).parameters
        mp = _ManualMonkeypatch() if "monkeypatch" in params else None
        try:
            fn(mp) if mp is not None else fn()
            print(f"PASS {name}")
            passed += 1
        except Exception:  # noqa: BLE001
            print(f"FAIL {name}")
            traceback.print_exc()
            failed += 1
        finally:
            if mp is not None:
                mp.undo()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        import pytest  # noqa: F401
    except ImportError:
        raise SystemExit(_run_manually())
    else:
        raise SystemExit(pytest.main([__file__, "-v"]))
