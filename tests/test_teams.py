import pytest

from mlb_odds import teams

MLB_FULL_NAMES = [
    "Arizona Diamondbacks", "Atlanta Braves", "Baltimore Orioles", "Boston Red Sox",
    "Chicago Cubs", "Chicago White Sox", "Cincinnati Reds", "Cleveland Guardians",
    "Colorado Rockies", "Detroit Tigers", "Houston Astros", "Kansas City Royals",
    "Los Angeles Angels", "Los Angeles Dodgers", "Miami Marlins", "Milwaukee Brewers",
    "Minnesota Twins", "New York Mets", "New York Yankees", "Athletics",
    "Philadelphia Phillies", "Pittsburgh Pirates", "San Diego Padres",
    "San Francisco Giants", "Seattle Mariners", "St. Louis Cardinals",
    "Tampa Bay Rays", "Texas Rangers", "Toronto Blue Jays", "Washington Nationals",
]

NFL_FULL_NAMES = [
    "Arizona Cardinals", "Atlanta Falcons", "Baltimore Ravens", "Buffalo Bills",
    "Carolina Panthers", "Chicago Bears", "Cincinnati Bengals", "Cleveland Browns",
    "Dallas Cowboys", "Denver Broncos", "Detroit Lions", "Green Bay Packers",
    "Houston Texans", "Indianapolis Colts", "Jacksonville Jaguars", "Kansas City Chiefs",
    "Los Angeles Chargers", "Los Angeles Rams", "Las Vegas Raiders", "Miami Dolphins",
    "Minnesota Vikings", "New England Patriots", "New Orleans Saints", "New York Giants",
    "New York Jets", "Philadelphia Eagles", "Pittsburgh Steelers", "Seattle Seahawks",
    "San Francisco 49ers", "Tampa Bay Buccaneers", "Tennessee Titans",
    "Washington Commanders",
]


def test_all_30_mlb_clubs_covered_for_the_odds_api():
    mapped = {teams.normalize("mlb", "the_odds_api", name) for name in MLB_FULL_NAMES}
    assert mapped == teams.MLB_CODES
    assert len(mapped) == 30


def test_all_32_nfl_clubs_covered_for_both_providers():
    for provider in ("the_odds_api", "espn"):
        mapped = {teams.normalize("nfl", provider, name) for name in NFL_FULL_NAMES}
        assert mapped == teams.NFL_CODES
        assert len(mapped) == 32


def test_mapping_values_are_canonical_per_sport():
    for (sport, _provider), mapping in teams._PROVIDER_MAPPINGS.items():
        assert set(mapping.values()) <= teams.CANONICAL_CODES[sport]


def test_shared_codes_are_namespaced_by_sport():
    """KC is the Royals in MLB and the Chiefs in NFL — same code, different
    franchise; cross-sport separation is the per-sport database (D-019)."""
    assert teams.normalize("mlb", "the_odds_api", "Kansas City Royals") == "KC"
    assert teams.normalize("nfl", "the_odds_api", "Kansas City Chiefs") == "KC"


def test_oakland_alias():
    assert teams.normalize("mlb", "the_odds_api", "Oakland Athletics") == "ATH"


def test_unknown_team_raises():
    with pytest.raises(teams.TeamLookupError):
        teams.normalize("mlb", "the_odds_api", "Montreal Expos")
    with pytest.raises(teams.TeamLookupError):
        teams.normalize("nfl", "the_odds_api", "Oakland Raiders")


def test_unknown_sport_or_provider_raises():
    with pytest.raises(teams.TeamLookupError):
        teams.normalize("mlb", "nope", "New York Yankees")
    with pytest.raises(teams.TeamLookupError):
        teams.normalize("nhl", "the_odds_api", "Boston Bruins")
