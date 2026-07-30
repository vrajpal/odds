"""Player-prop tests (D-018). The events fixture and the betrivers ladder are a
trimmed live recording (2026-07-30); the draftkings pitcher_strikeouts book in
the props fixture is an edited addition for over/under coverage."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from typer.testing import CliRunner

from conftest import FIXTURES, make_game_odds
from mlb_odds.cli import app
from mlb_odds.client import OddsClient
from mlb_odds.models import Quote
from mlb_odds.providers import TheOddsAPI
from mlb_odds.providers.base import ProviderError
from mlb_odds.storage import Storage

runner = CliRunner()


def props_transport() -> httpx.MockTransport:
    """Route the events list and per-event odds to their recorded fixtures."""
    events = json.loads((FIXTURES / "odds_api_events.json").read_text())
    props = json.loads((FIXTURES / "odds_api_event_props.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"x-requests-remaining": "480", "x-requests-used": "20"}
        path = request.url.path
        if path.endswith("/events"):
            # one event with props; the rest return empty books (finished games)
            return httpx.Response(200, json=events[:2], headers=headers)
        if path.endswith(f"/events/{props['id']}/odds"):
            return httpx.Response(200, json=props, headers=headers)
        if "/events/" in path and path.endswith("/odds"):
            empty = dict(props, id=path.split("/")[-2], bookmakers=[])
            return httpx.Response(200, json=empty, headers=headers)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


# ---- model validation ----


def test_prop_quote_requires_player_line_and_over_under():
    ok = Quote(
        book="dk", market="pitcher_strikeouts", outcome="over",
        line=6.5, price=-120, player="Jacob deGrom",
    )
    assert ok.player == "Jacob deGrom"

    with pytest.raises(ValueError, match="requires a player"):
        Quote(book="dk", market="pitcher_strikeouts", outcome="over", line=6.5, price=-120)
    with pytest.raises(ValueError, match="over/under"):
        Quote(
            book="dk", market="pitcher_strikeouts", outcome="home",
            line=6.5, price=-120, player="Jacob deGrom",
        )
    with pytest.raises(ValueError, match="requires a line"):
        Quote(book="dk", market="batter_home_runs", outcome="over", price=600, player="X")


def test_game_quote_rejects_player():
    with pytest.raises(ValueError, match="cannot carry a player"):
        Quote(book="dk", market="moneyline", outcome="home", price=-140, player="Aaron Judge")


# ---- provider ----


def test_fetch_player_props_parses_ladders_and_normalizes():
    provider = TheOddsAPI(api_key="test-key", transport=props_transport())
    results = provider.fetch_player_props(["batter_home_runs", "pitcher_strikeouts"])

    assert len(results) == 1  # the empty-bookmakers event is dropped
    (go,) = results
    assert go.game.away_team == "TEX"
    assert go.game.home_team == "TB"
    assert go.provider == "the_odds_api"

    ladders = [q for q in go.quotes if q.book == "betrivers"]
    assert all(q.market == "batter_home_runs" and q.outcome == "over" for q in ladders)
    caminero = [q for q in ladders if q.player == "Junior Caminero"]
    assert {q.line for q in caminero} >= {1.5, 2.5}  # ladder: several lines, one player

    ks = {(q.player, q.outcome, q.line): q.price for q in go.quotes if q.book == "draftkings"}
    assert ks[("Jacob deGrom", "over", 6.5)] == -125
    assert ks[("Jacob deGrom", "under", 6.5)] == -105

    assert provider.quota_remaining == 480


def test_fetch_player_props_rejects_unknown_market():
    provider = TheOddsAPI(api_key="test-key", transport=props_transport())
    with pytest.raises(ProviderError, match="unsupported prop market"):
        provider.fetch_player_props(["batter_stolen_bases"])


# ---- storage semantics ----


def _prop(player: str, line: float, price: int) -> Quote:
    return Quote(
        book="dk", market="pitcher_strikeouts", outcome="over",
        line=line, price=price, player=player,
    )


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "props.sqlite")
    yield s
    s.close()


def test_props_round_trip_and_history(storage):
    go = make_game_odds(quotes=[_prop("Jacob deGrom", 6.5, -120), _prop("Shane Baz", 5.5, 100)])
    storage.store([go])

    rows = storage.history_rows(go.game.game_id)
    players = {row[7] for row in rows}
    assert players == {"Jacob deGrom", "Shane Baz"}


def test_props_never_appear_on_boards(storage):
    start = datetime(2026, 7, 9, 23, 5, tzinfo=UTC)
    game_line = Quote(book="dk", market="moneyline", outcome="home", price=-140)
    storage.store(
        [
            make_game_odds(
                start_time=start,
                fetched_at=start - timedelta(hours=1),
                quotes=[game_line, _prop("Jacob deGrom", 6.5, -120)],
            )
        ]
    )

    (latest,) = storage.latest_odds()
    assert [q.market for q in latest.quotes] == ["moneyline"]
    (closing,) = storage.closing_odds()
    assert [q.market for q in closing.quotes] == ["moneyline"]


def test_changed_only_keeps_identical_ladder_out_but_writes_price_moves(storage):
    ladder = [_prop("Jacob deGrom", 6.5, -120), _prop("Jacob deGrom", 7.5, 145)]
    t1 = make_game_odds(fetched_at=datetime(2026, 7, 9, 15, 0, tzinfo=UTC), quotes=ladder)
    assert storage.store([t1], changed_only=True) == 2

    # identical ladder re-polled: nothing written (rung-level identity, D-018)
    t2 = make_game_odds(fetched_at=datetime(2026, 7, 9, 16, 0, tzinfo=UTC), quotes=ladder)
    assert storage.store([t2], changed_only=True) == 0

    # one rung's price moves: exactly that rung is appended
    moved = [_prop("Jacob deGrom", 6.5, -130), _prop("Jacob deGrom", 7.5, 145)]
    t3 = make_game_odds(fetched_at=datetime(2026, 7, 9, 17, 0, tzinfo=UTC), quotes=moved)
    assert storage.store([t3], changed_only=True) == 1


# ---- client + CLI ----


def test_client_skips_providers_without_prop_support(tmp_path):
    from conftest import FakeProvider

    client = OddsClient(providers=[FakeProvider()], db=tmp_path / "x.sqlite")
    assert client.fetch_and_store_props(["pitcher_strikeouts"]) == []
    assert client.last_errors == {}
    client.close()


def test_props_command_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("THE_ODDS_API_KEY", "test-key")
    monkeypatch.setattr(
        "mlb_odds.cli.TheOddsAPI",
        lambda: TheOddsAPI(api_key="test-key", transport=props_transport()),
    )
    db = tmp_path / "props.sqlite"

    result = runner.invoke(
        app,
        ["props", "--market", "pitcher_strikeouts", "--market", "batter_home_runs",
         "--db", str(db)],
    )

    assert result.exit_code == 0
    assert "credits remaining: 480" in result.output
    storage = Storage(db)
    players = {
        row[0]
        for row in storage._conn.execute(
            "SELECT DISTINCT player FROM odds WHERE player IS NOT NULL"
        )
    }
    storage.close()
    assert "Junior Caminero" in players
    assert "Jacob deGrom" in players


def test_props_command_rejects_unknown_market(tmp_path, monkeypatch):
    monkeypatch.setenv("THE_ODDS_API_KEY", "test-key")
    result = runner.invoke(
        app, ["props", "--market", "batter_walks", "--db", str(tmp_path / "x.sqlite")]
    )
    assert result.exit_code == 2
    assert "unsupported prop market" in result.output
