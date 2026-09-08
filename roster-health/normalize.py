"""Normalization helpers shared by every adapter.

Two jobs:

* Map each source's free-form status strings onto the :class:`Status` enum.
* Canonicalize team codes (ESPN, NFL.com and Sleeper disagree — WSH vs WAS,
  JAX vs JAC, LV vs OAK, ...), so the crosswalk can join on team.
"""

from __future__ import annotations

import re

from models import Status

# --------------------------------------------------------------------------- #
# Team code canonicalization
# --------------------------------------------------------------------------- #
# Canonical form is the Sleeper/most-common short code. Add aliases as needed.
_TEAM_ALIASES: dict[str, str] = {
    "WSH": "WAS", "WFT": "WAS",
    "JAC": "JAX",
    "LA": "LAR", "STL": "LAR",
    "SD": "LAC",
    "OAK": "LV", "LVR": "LV", "RAI": "LV",
    "ARZ": "ARI",
    "GNB": "GB",
    "KAN": "KC",
    "NWE": "NE", "NEP": "NE",
    "NOR": "NO",
    "SFO": "SF",
    "TAM": "TB",
    "CLV": "CLE",
    "BLT": "BAL",
    "HST": "HOU",
    "TEN": "TEN", "OTI": "TEN",
}

_VALID_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "KC", "LV", "LAC", "LAR", "MIA",
    "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SF", "SEA", "TB",
    "TEN", "WAS",
}


def canonical_team(team: str | None) -> str:
    if not team:
        return "FA"
    t = team.strip().upper()
    t = _TEAM_ALIASES.get(t, t)
    return t if t in _VALID_TEAMS else (team.strip().upper() or "FA")


# --------------------------------------------------------------------------- #
# Status normalization
# --------------------------------------------------------------------------- #
# Keys are matched case-insensitively against the raw source string.
_STATUS_MAP: dict[str, Status] = {
    "active": Status.ACTIVE,
    "": Status.ACTIVE,
    "probable": Status.ACTIVE,
    "full": Status.ACTIVE,               # full practice participation
    "questionable": Status.QUESTIONABLE,
    "q": Status.QUESTIONABLE,
    "limited": Status.QUESTIONABLE,      # limited participation ~ questionable-ish
    "doubtful": Status.DOUBTFUL,
    "d": Status.DOUBTFUL,
    "dnp": Status.DOUBTFUL,              # did-not-practice, lean doubtful
    "out": Status.OUT,
    "o": Status.OUT,
    "inactive": Status.OUT,
    "suspended": Status.OUT,
    "sus": Status.OUT,
    "ir": Status.IR,
    "injured reserve": Status.IR,
    "ir-r": Status.IR,
    "pup": Status.IR,
    "nfi": Status.IR,
    "reserve/pup": Status.IR,
    "reserve/covid-19": Status.OUT,
    "cov": Status.OUT,
    "did not practice": Status.DOUBTFUL,
}


def normalize_status(raw: str | None) -> Status:
    """Best-effort mapping of any source's status string onto the enum."""
    if raw is None:
        return Status.ACTIVE
    key = raw.strip().lower()
    if key in _STATUS_MAP:
        return _STATUS_MAP[key]
    # Substring fallbacks for messier strings ("Questionable - hamstring").
    for token, status in _STATUS_MAP.items():
        if token and token in key:
            return status
    return Status.UNKNOWN


# --------------------------------------------------------------------------- #
# Name normalization (used by the crosswalk's fuzzy fallback)
# --------------------------------------------------------------------------- #
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_PUNCT = re.compile(r"[^a-z0-9 ]+")


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation and Jr/Sr/III suffixes, collapse spaces."""
    n = _PUNCT.sub(" ", name.lower())
    parts = [p for p in n.split() if p not in _SUFFIXES]
    return " ".join(parts).strip()
