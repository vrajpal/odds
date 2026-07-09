from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mlb_odds.models import Game, Quote, make_game_id


def test_game_id_format():
    assert make_game_id("2026-07-09", "NYM", "NYY") == "2026-07-09-NYM-NYY-1"
    assert make_game_id("2026-07-09", "NYM", "NYY", 2) == "2026-07-09-NYM-NYY-2"


def test_naive_start_time_rejected():
    with pytest.raises(ValidationError):
        Game(
            game_id="x",
            start_time=datetime(2026, 7, 9, 23, 5),  # no tzinfo
            home_team="NYY",
            away_team="NYM",
        )


def test_season_derived_from_start_time():
    game = Game(
        game_id="x",
        start_time=datetime(2026, 7, 9, 23, 5, tzinfo=UTC),
        home_team="NYY",
        away_team="NYM",
    )
    assert game.season == 2026


@pytest.mark.parametrize(
    ("american", "decimal"),
    [(-110, 1.909), (100, 2.0), (150, 2.5), (-200, 1.5), (250, 3.5)],
)
def test_price_decimal(american, decimal):
    quote = Quote(book="draftkings", market="moneyline", outcome="home", price=american)
    assert quote.price_decimal == pytest.approx(decimal, abs=1e-3)


@pytest.mark.parametrize("bad", [0, 50, -99, 99])
def test_invalid_american_odds_rejected(bad):
    with pytest.raises(ValidationError):
        Quote(book="draftkings", market="moneyline", outcome="home", price=bad)
