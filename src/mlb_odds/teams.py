"""Canonical team codes per sport and per-provider name normalization.

Every provider maps its raw team names through normalize(); canonical codes are the
only team representation allowed past the provider boundary. Codes are namespaced
by sport (MLB KC and NFL KC are different franchises) — cross-sport game_id
collisions are prevented by one database per sport, not by the codes (D-019).
"""

MLB_CODES = frozenset(
    {
        "ARI", "ATL", "BAL", "BOS", "CHC", "CWS", "CIN", "CLE", "COL", "DET",
        "HOU", "KC", "LAA", "LAD", "MIA", "MIL", "MIN", "NYM", "NYY", "ATH",
        "PHI", "PIT", "SD", "SEA", "SF", "STL", "TB", "TEX", "TOR", "WSH",
    }
)

NFL_CODES = frozenset(
    {
        "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
        "DET", "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA",
        "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
        "TEN", "WAS",
    }
)

CANONICAL_CODES: dict[str, frozenset[str]] = {"mlb": MLB_CODES, "nfl": NFL_CODES}

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

# Both The Odds API and ESPN's displayName use full NFL club names.
_NFL_FULL_NAMES = {
    "Arizona Cardinals": "ARI",
    "Atlanta Falcons": "ATL",
    "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR",
    "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN",
    "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN",
    "Detroit Lions": "DET",
    "Green Bay Packers": "GB",
    "Houston Texans": "HOU",
    "Indianapolis Colts": "IND",
    "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC",
    "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR",
    "Las Vegas Raiders": "LV",
    "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN",
    "New England Patriots": "NE",
    "New Orleans Saints": "NO",
    "New York Giants": "NYG",
    "New York Jets": "NYJ",
    "Philadelphia Eagles": "PHI",
    "Pittsburgh Steelers": "PIT",
    "Seattle Seahawks": "SEA",
    "San Francisco 49ers": "SF",
    "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN",
    "Washington Commanders": "WAS",
}

_PROVIDER_MAPPINGS: dict[tuple[str, str], dict[str, str]] = {
    ("mlb", "the_odds_api"): _THE_ODDS_API,
    # ESPN's team.displayName uses the same full club names.
    ("mlb", "espn"): _THE_ODDS_API,
    ("nfl", "the_odds_api"): _NFL_FULL_NAMES,
    ("nfl", "espn"): _NFL_FULL_NAMES,
}


class TeamLookupError(KeyError):
    """A provider returned a team name we don't recognize."""


def normalize(sport: str, provider: str, raw_name: str) -> str:
    """Map a provider's raw team name to a canonical code for one sport.

    Raises TeamLookupError for unknown names — callers decide whether that is
    fatal (tests/strict mode) or a logged skip (production).
    """
    try:
        mapping = _PROVIDER_MAPPINGS[(sport, provider)]
    except KeyError:
        raise TeamLookupError(
            f"no team mapping registered for {sport!r}/{provider!r}"
        ) from None
    try:
        return mapping[raw_name]
    except KeyError:
        raise TeamLookupError(
            f"unknown team name {raw_name!r} from {sport!r}/{provider!r}"
        ) from None


# NFL divisions (2026 alignment), canonical codes — used by the contest tool
# for divisional-game flags (D-025). League facts, so they live with the codes.
NFL_DIVISIONS: dict[str, str] = {
    "BUF": "AFC East", "MIA": "AFC East", "NE": "AFC East", "NYJ": "AFC East",
    "BAL": "AFC North", "CIN": "AFC North", "CLE": "AFC North", "PIT": "AFC North",
    "HOU": "AFC South", "IND": "AFC South", "JAX": "AFC South", "TEN": "AFC South",
    "DEN": "AFC West", "KC": "AFC West", "LAC": "AFC West", "LV": "AFC West",
    "DAL": "NFC East", "NYG": "NFC East", "PHI": "NFC East", "WAS": "NFC East",
    "CHI": "NFC North", "DET": "NFC North", "GB": "NFC North", "MIN": "NFC North",
    "ATL": "NFC South", "CAR": "NFC South", "NO": "NFC South", "TB": "NFC South",
    "ARI": "NFC West", "LAR": "NFC West", "SEA": "NFC West", "SF": "NFC West",
}
