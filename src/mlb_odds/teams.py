"""Canonical MLB team codes and per-provider name normalization.

Every provider maps its raw team names through normalize(); canonical codes are the
only team representation allowed past the provider boundary.
"""

CANONICAL_CODES = frozenset(
    {
        "ARI", "ATL", "BAL", "BOS", "CHC", "CWS", "CIN", "CLE", "COL", "DET",
        "HOU", "KC", "LAA", "LAD", "MIA", "MIL", "MIN", "NYM", "NYY", "ATH",
        "PHI", "PIT", "SD", "SEA", "SF", "STL", "TB", "TEX", "TOR", "WSH",
    }
)

# The Odds API uses full club names.
_THE_ODDS_API = {
    "Arizona Diamondbacks": "ARI",
    "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL",
    "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC",
    "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN",
    "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL",
    "Detroit Tigers": "DET",
    "Houston Astros": "HOU",
    "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA",
    "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA",
    "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN",
    "New York Mets": "NYM",
    "New York Yankees": "NYY",
    # The club dropped "Oakland" in 2025; accept both spellings.
    "Athletics": "ATH",
    "Oakland Athletics": "ATH",
    "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SD",
    "San Francisco Giants": "SF",
    "Seattle Mariners": "SEA",
    "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB",
    "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR",
    "Washington Nationals": "WSH",
    # Alt spelling seen in some feeds.
    "St Louis Cardinals": "STL",
}

_PROVIDER_MAPPINGS: dict[str, dict[str, str]] = {
    "the_odds_api": _THE_ODDS_API,
    # ESPN's team.displayName uses the same full club names.
    "espn": _THE_ODDS_API,
}


class TeamLookupError(KeyError):
    """A provider returned a team name we don't recognize."""


def normalize(provider: str, raw_name: str) -> str:
    """Map a provider's raw team name to a canonical code.

    Raises TeamLookupError for unknown names — callers decide whether that is
    fatal (tests/strict mode) or a logged skip (production).
    """
    try:
        mapping = _PROVIDER_MAPPINGS[provider]
    except KeyError:
        raise TeamLookupError(f"no team mapping registered for provider {provider!r}") from None
    try:
        return mapping[raw_name]
    except KeyError:
        raise TeamLookupError(
            f"unknown team name {raw_name!r} from provider {provider!r}"
        ) from None
