"""Betting-model composition tests (D-036): margin->prob conversion, the NFL
two-lens blend, model EV on the dashboard, and MLB behavior unchanged."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import make_game_odds, make_nfl_spread_odds
from mlb_odds import api
from mlb_odds.model import blend_logits, margin_to_prob, nfl_model_prob
from mlb_odds.models import Quote
from mlb_odds.storage import Storage


def test_margin_to_prob_anchors():
    # Pick'em is a coin flip; the classic anchors hold within a point.
    assert margin_to_prob(0.0) == 0.5
    assert 0.57 < margin_to_prob(3.0) < 0.61   # -3 favorite ~59%
    assert 0.69 < margin_to_prob(7.0) < 0.72   # -7 favorite ~70%
    assert margin_to_prob(-3.0) == pytest.approx(1 - margin_to_prob(3.0))


def test_blend_logits_renormalizes_over_present():
    assert blend_logits([None, 1.0], [0.5, 0.5]) == pytest.approx(1.0)
    assert blend_logits([0.0, 1.0], [0.5, 0.5]) == pytest.approx(0.5)
    assert blend_logits([None, None], [0.5, 0.5]) is None


def test_nfl_model_prob_blends_both_lenses():
    blended, spread_prob = nfl_model_prob(0.60, -3.0)
    assert spread_prob == pytest.approx(margin_to_prob(3.0))
    assert min(0.60, spread_prob) < blended < max(0.60, spread_prob)
    # One lens missing: use the other alone.
    only_ml, none_spread = nfl_model_prob(0.60, None)
    assert only_ml == pytest.approx(0.60, abs=1e-3) and none_spread is None
    only_spread, sp = nfl_model_prob(None, -7.0)
    assert only_spread == pytest.approx(sp)


class TestNflDashboardModel:
    @pytest.fixture
    def nfl_client(self, tmp_path, monkeypatch):
        db = tmp_path / "nfl-odds.sqlite"
        storage = Storage(db)
        fetch = datetime(2026, 8, 1, 17, 0, tzinfo=UTC)
        start = datetime(2026, 8, 2, 17, 0, tzinfo=UTC)
        # Round-robin spreads + moneylines: enough for both fits.
        spreads = {("KC", "BUF"): -3.0, ("BUF", "KC"): -1.0, ("KC", "SF"): -6.5,
                   ("SF", "KC"): 2.5, ("BUF", "SF"): -4.0, ("SF", "BUF"): 1.5,
                   ("KC", "DET"): -7.5, ("DET", "KC"): 4.0, ("BUF", "DET"): -5.0,
                   ("DET", "BUF"): 2.0, ("SF", "DET"): -2.0, ("DET", "SF"): 0.0}
        local_noon = datetime.now().astimezone().replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        for (home, away), spread in spreads.items():
            go = make_nfl_spread_odds({"circa": spread}, fetch, away=away, home=home,
                                      start_time=start)
            ml_home = -140 if spread < 0 else 110
            go.quotes.append(Quote(book="circa", market="moneyline", outcome="home",
                                   price=ml_home))
            go.quotes.append(Quote(book="circa", market="moneyline", outcome="away",
                                   price=120 if spread < 0 else -130))
            storage.store([go])
            start += timedelta(hours=4)
        # Today's game so the dashboard window finds it.
        today = make_nfl_spread_odds({"circa": -3.5}, fetch, away="BUF", home="KC",
                                     start_time=local_noon.astimezone(UTC))
        today.quotes.append(Quote(book="circa", market="moneyline", outcome="home", price=-160))
        today.quotes.append(Quote(book="circa", market="moneyline", outcome="away", price=140))
        storage.store([today])
        storage.close()
        monkeypatch.setenv("NFL_ODDS_DB", str(db))
        monkeypatch.setenv("MLB_ODDS_DB", str(tmp_path / "mlb.sqlite"))
        Storage(tmp_path / "mlb.sqlite").close()
        return TestClient(api.app, raise_server_exceptions=False), today.game.game_id

    def test_nfl_game_gets_two_lens_model_and_margin(self, nfl_client):
        client, gid = nfl_client
        body = client.get("/api/dashboard", params={"sport": "nfl"}).json()
        (game,) = [g for g in body["games"] if g["game_id"] == gid]
        ml = game["moneyline"]
        assert ml["market_model_prob"] is not None
        assert ml["spread_model_prob"] is not None
        assert ml["statcast_prob"] is None  # no Statcast lens in NFL
        lo = min(ml["market_model_prob"], ml["spread_model_prob"])
        hi = max(ml["market_model_prob"], ml["spread_model_prob"])
        assert lo <= ml["model_prob"] <= hi
        assert game["predicted_margin"] is not None
        # KC rated best of the league: model favors the home side.
        assert game["predicted_margin"] > 0
        assert ml["best_home"]["model_ev"] is not None
        assert ml["best_away"]["model_ev"] is not None

    def test_model_ev_uses_model_probability(self, nfl_client):
        client, gid = nfl_client
        (game,) = [
            g for g in client.get("/api/dashboard", params={"sport": "nfl"}).json()["games"]
            if g["game_id"] == gid
        ]
        ml = game["moneyline"]
        from mlb_odds.valuation import expected_value

        assert ml["best_home"]["model_ev"] == pytest.approx(
            expected_value(ml["model_prob"], ml["best_home"]["price"]), abs=1e-4
        )


class TestCircaModelSurfaces:
    """D-036 on the contest pages: two-lens win probs on both boards."""

    @pytest.fixture
    def circa_env(self, tmp_path, monkeypatch):
        from mlb_odds import contest_api

        db = tmp_path / "nfl-odds.sqlite"
        storage = Storage(db)
        fetch = datetime(2026, 8, 1, 17, 0, tzinfo=UTC)
        start = datetime(2026, 8, 2, 17, 0, tzinfo=UTC)
        spreads = {("KC", "BUF"): -3.0, ("BUF", "SF"): -4.0, ("SF", "KC"): 2.5,
                   ("KC", "SF"): -6.5, ("BUF", "KC"): -1.0, ("SF", "BUF"): 1.5,
                   ("KC", "DET"): -7.5, ("DET", "BUF"): 2.0, ("SF", "DET"): -2.0,
                   ("DET", "KC"): 4.0, ("BUF", "DET"): -5.0, ("DET", "SF"): 0.0}
        for (home, away), spread in spreads.items():
            go = make_nfl_spread_odds({"circa": spread}, fetch, away=away, home=home,
                                      start_time=start)
            go.quotes.append(Quote(book="circa", market="moneyline", outcome="home",
                                   price=-150 if spread < 0 else 120))
            go.quotes.append(Quote(book="circa", market="moneyline", outcome="away",
                                   price=130 if spread < 0 else -140))
            storage.store([go])
            start += timedelta(hours=4)
        # A week-1 game for the contest board and survivor leg 1.
        wk1 = make_nfl_spread_odds({"circa": -3.5}, fetch, away="BUF", home="KC",
                                   start_time=datetime(2026, 9, 13, 17, 0, tzinfo=UTC))
        wk1.quotes.append(Quote(book="circa", market="moneyline", outcome="home", price=-170))
        wk1.quotes.append(Quote(book="circa", market="moneyline", outcome="away", price=150))
        storage.store([wk1])
        storage.close()
        monkeypatch.setenv("NFL_ODDS_DB", str(db))
        monkeypatch.setenv("CONTEST_DB", str(tmp_path / "contest.sqlite"))
        monkeypatch.setenv("CONTEST_MEMBERS", "vijai,sam,alex")
        return TestClient(contest_api.app, raise_server_exceptions=False), wk1.game.game_id

    def test_million_board_carries_model(self, circa_env):
        client, gid = circa_env
        (game,) = [
            g for g in client.get("/api/contest/board", params={"week": 1}).json()["games"]
            if g["game_id"] == gid
        ]
        assert game["model_win_prob"] is not None
        assert game["ml_lens_prob"] is not None
        assert game["spread_lens_prob"] is not None
        lo = min(game["ml_lens_prob"], game["spread_lens_prob"])
        hi = max(game["ml_lens_prob"], game["spread_lens_prob"])
        assert lo <= game["model_win_prob"] <= hi

    def test_survivor_board_market_prob_prefers_devigged_ml(self, circa_env):
        from mlb_odds.valuation import devig_pair

        client, gid = circa_env
        body = client.get(
            "/api/survivor/board", params={"leg": "1"}
        ).json()
        (game,) = [g for g in body["games"] if g["game_id"] == gid]
        # Books quote the moneyline: market win prob is the devig, not the
        # spread conversion.
        assert game["home_win_prob"] == pytest.approx(devig_pair(-170, 150), abs=1e-3)
        assert game["model_win_prob"] is not None
        assert game["ml_lens_prob"] is not None and game["spread_lens_prob"] is not None


def test_projection_lens_shifts_dashboard_model(tmp_path, monkeypatch):
    """D-037: an imported projection becomes a third lens in the blend."""
    from mlb_odds.models import Quote as Q

    db = tmp_path / "odds.sqlite"
    storage = Storage(db)
    t = datetime(2026, 7, 20, 23, 0, tzinfo=UTC)
    for i in range(10):
        teams4 = ["NYY", "BOS", "TB", "BAL"]
        home, away = teams4[i % 4], teams4[(i + 1) % 4]
        storage.store([make_game_odds(
            away=away, home=home, start_time=t, fetched_at=t - timedelta(hours=8),
            quotes=[Q(book="dk", market="moneyline", outcome="home", price=-140),
                    Q(book="dk", market="moneyline", outcome="away", price=120)],
        )])
        t += timedelta(hours=27)
    local_noon = datetime.now().astimezone().replace(hour=12, minute=0, second=0,
                                                     microsecond=0)
    today = make_game_odds(
        away="BOS", home="NYY", start_time=local_noon.astimezone(UTC),
        fetched_at=datetime.now(UTC) - timedelta(hours=2),
        quotes=[Q(book="dk", market="moneyline", outcome="home", price=-150),
                Q(book="dk", market="moneyline", outcome="away", price=130)],
    )
    storage.store([today])
    storage.close()
    monkeypatch.setenv("MLB_ODDS_DB", str(db))
    client = TestClient(api.app, raise_server_exceptions=False)

    def game():
        return next(g for g in client.get("/api/dashboard").json()["games"]
                    if g["game_id"] == today.game.game_id)

    before = game()["moneyline"]
    assert before["projection_prob"] is None
    baseline = before["model_prob"]

    # Import a projection far above the market's opinion.
    storage = Storage(db)
    storage.add_projection(today.game.game_id, "fanduel_research",
                           fetched_at=datetime.now(UTC), home_win_prob=0.80,
                           away_score=None, home_score=None)
    storage.close()
    after = game()["moneyline"]
    assert after["projection_prob"] == pytest.approx(0.80)
    assert after["projection_source"] == "fanduel_research"
    assert after["model_prob"] > baseline  # pulled toward the projection
    assert after["model_prob"] < 0.80  # but not all the way


def test_projection_report_brier(tmp_path, monkeypatch):
    from mlb_odds.models import Quote as Q

    db = tmp_path / "odds.sqlite"
    storage = Storage(db)
    start = datetime(2026, 7, 9, 23, 5, tzinfo=UTC)
    go = make_game_odds(start_time=start,
                        quotes=[Q(book="dk", market="moneyline", outcome="home", price=-140),
                                Q(book="dk", market="moneyline", outcome="away", price=120)])
    storage.store([go])
    storage.add_projection(go.game.game_id, "fanduel_research",
                           fetched_at=start - timedelta(hours=2),
                           home_win_prob=0.70, away_score=None, home_score=None)
    storage.record_result(go.game.game_id, 6, 2, fetched_at=start + timedelta(hours=4))
    storage.close()
    monkeypatch.setenv("MLB_ODDS_DB", str(db))
    client = TestClient(api.app, raise_server_exceptions=False)
    report = client.get("/api/projections/report").json()
    assert report["n"] == 1
    assert report["hit_rate"] == 1.0  # said home 70%, home won 6-2
    assert report["brier"] == pytest.approx((0.70 - 1) ** 2, abs=1e-4)
