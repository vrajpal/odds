"""NFL support tests (D-019) against live-recorded fixtures (2026-07-31):
The Odds API game lines (3 events, 3 books each) and ESPN's scoreboard
(the Hall of Fame preseason game, full odds)."""

import pytest
from typer.testing import CliRunner

from conftest import FakeProvider, fixture_transport, make_game_odds
from mlb_odds.cli import _resolve_db, app
from mlb_odds.client import OddsClient
from mlb_odds.providers import ESPN, TheOddsAPI
from mlb_odds.providers.base import ProviderError
from mlb_odds.storage import Storage

runner = CliRunner()


# ---- The Odds API, sport="nfl" ----


def test_odds_api_nfl_parses_spread_not_run_line():
    provider = TheOddsAPI(
        api_key="test-key", sport="nfl", transport=fixture_transport("odds_api_nfl_normal")
    )
    results = provider.fetch_game_lines()

    assert len(results) == 3
    by_home = {go.game.home_team: go for go in results}
    assert set(by_home) == {"SEA", "LAR", "PIT"}

    ne_sea = by_home["SEA"]
    assert ne_sea.game.away_team == "NE"
    markets = {q.market for q in ne_sea.quotes}
    assert "spread" in markets
    assert "run_line" not in markets

    spread_home = next(
        q for q in ne_sea.quotes if q.market == "spread" and q.outcome == "home"
    )
    assert spread_home.line == -3.5  # Seahawks favored in the recorded lines


def test_odds_api_default_sport_is_still_mlb():
    provider = TheOddsAPI(api_key="test-key", transport=fixture_transport("normal_day"))
    results = provider.fetch_game_lines()
    markets = {q.market for go in results for q in go.quotes}
    assert "run_line" in markets
    assert "spread" not in markets


def test_nfl_props_rejected():
    provider = TheOddsAPI(api_key="test-key", sport="nfl")
    with pytest.raises(ProviderError, match="MLB only"):
        provider.fetch_player_props(["pitcher_strikeouts"])


# ---- ESPN, sport="nfl" ----


def test_espn_nfl_parses_hall_of_fame_game():
    provider = ESPN(sport="nfl", transport=fixture_transport("espn_nfl_scoreboard_normal"))
    (go,) = provider.fetch_game_lines()

    assert go.game.home_team == "ARI"
    assert go.game.away_team == "CAR"
    quotes = {(q.market, q.outcome): q for q in go.quotes}
    assert quotes[("moneyline", "home")].price == 105
    assert quotes[("spread", "home")].line == 1.5
    assert quotes[("total", "over")].line == 36.5
    assert ("run_line", "home") not in quotes
    assert {q.book for q in go.quotes} == {"draftkings"}


# ---- per-sport databases ----


def test_default_db_paths_differ_per_sport(monkeypatch):
    from mlb_odds.cli import SportChoice

    monkeypatch.delenv("MLB_ODDS_DB", raising=False)
    monkeypatch.delenv("NFL_ODDS_DB", raising=False)
    assert str(_resolve_db(None, SportChoice.mlb)) == "odds.sqlite"
    assert str(_resolve_db(None, SportChoice.nfl)) == "nfl-odds.sqlite"

    monkeypatch.setenv("NFL_ODDS_DB", "/tmp/x/custom-nfl.sqlite")
    assert str(_resolve_db(None, SportChoice.nfl)) == "/tmp/x/custom-nfl.sqlite"
    assert str(_resolve_db(None, SportChoice.mlb)) == "odds.sqlite"  # env is per-sport


# ---- CLI end to end ----


def test_collect_sport_nfl_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("THE_ODDS_API_KEY", "test-key")
    monkeypatch.setattr(
        "mlb_odds.cli.TheOddsAPI",
        lambda **kw: TheOddsAPI(
            api_key="test-key", transport=fixture_transport("odds_api_nfl_normal"), **kw
        ),
    )
    db = tmp_path / "nfl.sqlite"

    result = runner.invoke(app, ["collect", "--once", "--sport", "nfl", "--db", str(db)])

    assert result.exit_code == 0
    storage = Storage(db)
    assert len(storage.games()) == 3
    markets = {
        row[0] for row in storage._conn.execute("SELECT DISTINCT market FROM odds")
    }
    storage.close()
    assert markets == {"moneyline", "spread", "total"}


def test_today_renders_spread_column_for_nfl(tmp_path, monkeypatch):
    """The NFL board's third column is the spread, labeled as such."""
    from datetime import UTC, datetime

    from mlb_odds.models import Game, GameOdds, Quote

    start = datetime.now().astimezone().replace(hour=19, minute=0, second=0, microsecond=0)
    game = Game(
        game_id=f"{start.astimezone(UTC).date()}-NE-SEA-1",
        start_time=start.astimezone(UTC),
        home_team="SEA",
        away_team="NE",
        provider_ids={"the_odds_api": "nfl-e2e"},
    )
    go = GameOdds(
        game=game,
        fetched_at=datetime.now(UTC),
        provider="the_odds_api",
        quotes=[
            Quote(book="draftkings", market="moneyline", outcome="away", price=164),
            Quote(book="draftkings", market="moneyline", outcome="home", price=-198),
            Quote(book="draftkings", market="spread", outcome="home", line=-3.5, price=-110),
            Quote(book="draftkings", market="spread", outcome="away", line=3.5, price=-110),
            Quote(book="draftkings", market="total", outcome="over", line=44.5, price=-110),
        ],
    )
    db = tmp_path / "nfl.sqlite"
    storage = Storage(db)
    storage.store([go])
    storage.close()

    result = runner.invoke(app, ["today", "--sport", "nfl", "--db", str(db)])

    assert result.exit_code == 0
    assert "NE @ SEA" in result.output
    assert "spread" in result.output
    assert "run line" not in result.output
    assert "-3.5 (-110)" in result.output
    assert "44.5 (o-110)" in result.output


def test_props_command_rejects_nfl(tmp_path, monkeypatch):
    monkeypatch.setenv("THE_ODDS_API_KEY", "test-key")
    result = runner.invoke(
        app,
        ["props", "--market", "pitcher_strikeouts", "--sport", "nfl",
         "--db", str(tmp_path / "x.sqlite")],
    )
    assert result.exit_code == 2
    assert "MLB only" in result.output


def test_sports_do_not_share_a_client_pipeline_regression(tmp_path):
    """Same canonical code (KC) in two sports: separate DBs keep identities
    apart — storing an NFL game never touches the MLB database."""
    mlb_db, nfl_db = tmp_path / "mlb.sqlite", tmp_path / "nfl.sqlite"

    mlb_client = OddsClient(providers=[FakeProvider()], db=mlb_db)
    mlb_client.fetch_and_store()
    mlb_client.close()

    nfl_storage = Storage(nfl_db)
    nfl_storage.store(
        [make_game_odds(away="KC", home="DEN", provider="the_odds_api")]
    )
    games = [g.game_id for g in nfl_storage.games()]
    nfl_storage.close()

    mlb_storage = Storage(mlb_db)
    assert len(mlb_storage.games()) == 1  # untouched by the NFL write
    mlb_storage.close()
    assert games == ["2026-07-09-KC-DEN-1"]
