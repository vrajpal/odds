"""Valuation math tests (D-030): devig, EV, moneyline pairing, and recovering
known team strengths from a synthetic moneyline market."""

from datetime import UTC, datetime, timedelta

import pytest

from conftest import make_game_odds
from mlb_odds.models import Quote
from mlb_odds.storage import Storage
from mlb_odds.valuation import (
    american_to_decimal,
    american_to_prob,
    best_prices,
    book_probs,
    consensus_prob,
    devig_pair,
    expected_value,
    implied_strengths,
    model_home_prob,
    moneyline_history,
)

T0 = datetime(2026, 8, 1, 15, 0, tzinfo=UTC)
START = datetime(2026, 8, 9, 23, 5, tzinfo=UTC)


def ml_odds(book_pairs, fetched_at, *, away="NYM", home="NYY", start=START):
    quotes = []
    for book, (home_price, away_price) in book_pairs.items():
        quotes.append(Quote(book=book, market="moneyline", outcome="home", price=home_price))
        quotes.append(Quote(book=book, market="moneyline", outcome="away", price=away_price))
    return make_game_odds(away=away, home=home, start_time=start, fetched_at=fetched_at,
                          quotes=quotes)


# --- primitive conversions ---


def test_american_conversions():
    assert american_to_prob(-150) == pytest.approx(0.6)
    assert american_to_prob(150) == pytest.approx(0.4)
    assert american_to_decimal(-150) == pytest.approx(1.6667, abs=1e-4)
    assert american_to_decimal(150) == pytest.approx(2.5)


def test_devig_removes_the_vig_symmetrically():
    # -110/-110: raw probs 0.524/0.524 sum to 1.048; devig -> exactly 0.5.
    assert devig_pair(-110, -110) == pytest.approx(0.5)
    # -150/+130: home favored; devigged prob strictly between raw and naive.
    p = devig_pair(-150, 130)
    assert 0.55 < p < 0.60


def test_expected_value_signs():
    # Fair 50%, getting +110 -> positive EV; laying -120 -> negative.
    assert expected_value(0.5, 110) == pytest.approx(0.05)
    assert expected_value(0.5, -120) < 0
    # A fair price at the fair prob is EV zero.
    assert expected_value(0.6, -150) == pytest.approx(0.0)


# --- history pairing and consensus ---


@pytest.fixture
def odds_db(tmp_path):
    storage = Storage(tmp_path / "odds.sqlite")
    yield storage
    storage.close()


def test_moneyline_history_pairs_and_drops_half_quotes(odds_db):
    go = ml_odds({"draftkings": (-150, 130), "fanduel": (-155, 135)}, T0)
    # A book quoting only one side must not produce a tick.
    go.quotes.append(Quote(book="halfbook", market="moneyline", outcome="home", price=-140))
    odds_db.store([go])
    ticks = moneyline_history(odds_db, go.game.game_id)
    assert {t.book for t in ticks} == {"draftkings", "fanduel"}
    dk = next(t for t in ticks if t.book == "draftkings")
    assert (dk.home_price, dk.away_price) == (-150, 130)
    assert dk.home_prob == pytest.approx(devig_pair(-150, 130))


def test_book_probs_carry_forward_and_consensus(odds_db):
    g1 = ml_odds({"draftkings": (-150, 130), "fanduel": (-160, 140)}, T0)
    odds_db.store([g1], changed_only=True)
    # Later snapshot: only draftkings moves.
    g2 = ml_odds({"draftkings": (-170, 150), "fanduel": (-160, 140)}, T0 + timedelta(hours=5))
    odds_db.store([g2], changed_only=True)

    ticks = moneyline_history(odds_db, g1.game.game_id)
    mid = book_probs(ticks, asof=T0 + timedelta(hours=1))
    assert mid["draftkings"].home_price == -150
    latest = book_probs(ticks)
    assert latest["draftkings"].home_price == -170
    assert latest["fanduel"].home_price == -160  # carried forward
    consensus = consensus_prob(latest)
    assert consensus is not None
    lo = min(latest["draftkings"].home_prob, latest["fanduel"].home_prob)
    hi = max(latest["draftkings"].home_prob, latest["fanduel"].home_prob)
    assert lo <= consensus <= hi
    assert consensus_prob({}) is None


def test_best_prices_pick_highest_ev_per_side(odds_db):
    go = ml_odds({"sharp": (-150, 140), "square": (-160, 125)}, T0)
    odds_db.store([go])
    pairs = book_probs(moneyline_history(odds_db, go.game.game_id))
    fair = consensus_prob(pairs)
    assert fair is not None
    home, away = best_prices(pairs, fair)
    assert home is not None and away is not None
    assert home.book == "sharp"  # -150 beats -160 on the same side
    assert away.book == "sharp"  # +140 beats +125
    assert home.ev > expected_value(fair, -160)


# --- strength fit: recover a synthetic league ---


def test_implied_strengths_recover_synthetic_league(tmp_path):
    import math

    storage = Storage(tmp_path / "odds.sqlite")
    true = {"NYY": 0.5, "BOS": 0.15, "TB": -0.15, "BAL": -0.5}
    hfa = 0.1
    start = START
    for home in true:
        for away in true:
            if home == away:
                continue
            logit = true[home] - true[away] + hfa
            p = 1 / (1 + math.exp(-logit))
            # American pair for that prob with a touch of juice on each side.
            if p >= 0.5:
                price_home = int(round(-100 * p / (1 - p))) - 10
                price_away = int(round(100 * p / (1 - p))) - 10
            else:
                price_home = int(round(100 * (1 - p) / p)) - 10
                price_away = int(round(-100 * (1 - p) / p)) - 10
            go = ml_odds({"draftkings": (price_home, price_away)}, T0,
                         away=away, home=home, start=start)
            storage.store([go])
            start += timedelta(hours=4)

    fitted = implied_strengths(storage)
    assert fitted is not None
    strengths, fit_hfa = fitted
    assert sorted(strengths, key=lambda t: -strengths[t]) == ["NYY", "BOS", "TB", "BAL"]
    assert fit_hfa == pytest.approx(hfa, abs=0.06)
    p = model_home_prob(strengths, fit_hfa, "NYY", "BAL")
    assert p is not None and p > 0.65
    assert model_home_prob(strengths, fit_hfa, "NYY", "XXX") is None
    storage.close()


def test_implied_strengths_need_enough_games(odds_db):
    odds_db.store([ml_odds({"draftkings": (-150, 130)}, T0)])
    assert implied_strengths(odds_db) is None
