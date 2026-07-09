import pytest

from mlb_odds import teams


def test_all_30_clubs_covered_for_the_odds_api():
    mapped = {
        teams.normalize("the_odds_api", name)
        for name in [
            "Arizona Diamondbacks", "Atlanta Braves", "Baltimore Orioles", "Boston Red Sox",
            "Chicago Cubs", "Chicago White Sox", "Cincinnati Reds", "Cleveland Guardians",
            "Colorado Rockies", "Detroit Tigers", "Houston Astros", "Kansas City Royals",
            "Los Angeles Angels", "Los Angeles Dodgers", "Miami Marlins", "Milwaukee Brewers",
            "Minnesota Twins", "New York Mets", "New York Yankees", "Athletics",
            "Philadelphia Phillies", "Pittsburgh Pirates", "San Diego Padres",
            "San Francisco Giants", "Seattle Mariners", "St. Louis Cardinals",
            "Tampa Bay Rays", "Texas Rangers", "Toronto Blue Jays", "Washington Nationals",
        ]
    }
    assert mapped == teams.CANONICAL_CODES
    assert len(mapped) == 30


def test_mapping_values_are_canonical():
    for mapping in teams._PROVIDER_MAPPINGS.values():
        assert set(mapping.values()) <= teams.CANONICAL_CODES


def test_oakland_alias():
    assert teams.normalize("the_odds_api", "Oakland Athletics") == "ATH"


def test_unknown_team_raises():
    with pytest.raises(teams.TeamLookupError):
        teams.normalize("the_odds_api", "Montreal Expos")


def test_unknown_provider_raises():
    with pytest.raises(teams.TeamLookupError):
        teams.normalize("nope", "New York Yankees")
