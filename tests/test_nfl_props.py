"""NFL player-prop tests (D-022). Event metadata is a live recording
(2026-08-06); the ladders are an edited fixture — NFL props are not posted
months out, so the response shape mirrors the recorded MLB one exactly."""

import json

import httpx
from typer.testing import CliRunner

from conftest import FIXTURES
from mlb_odds.cli import app
from mlb_odds.providers import TheOddsAPI
from mlb_odds.providers.base import ProviderError
from mlb_odds.storage import Storage

runner = CliRunner()


def nfl_props_transport() -> httpx.MockTransport:
    events = json.loads((FIXTURES / "odds_api_nfl_events.json").read_text())
    props = json.loads((FIXTURES / "odds_api_nfl_event_props.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"x-requests-remaining": "480", "x-requests-used": "20"}
        path = request.url.path
        assert "americanfootball_nfl" in path  # sport key routes the request
        if path.endswith("/events"):
            return httpx.Response(200, json=events, headers=headers)
        if path.endswith(f"/events/{props['id']}/odds"):
            return httpx.Response(200, json=props, headers=headers)
        if "/events/" in path and path.endswith("/odds"):
            empty = dict(props, id=path.split("/")[-2], bookmakers=[])
            return httpx.Response(200, json=empty, headers=headers)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def provider() -> TheOddsAPI:
    return TheOddsAPI(api_key="k", sport="nfl", transport=nfl_props_transport())


def test_nfl_prop_ladders_parse_and_normalize():
    (go,) = provider().fetch_player_props(["player_pass_yds", "player_pass_tds"])
    assert go.game.away_team == "NE" and go.game.home_team == "SEA"
    maye_yds = [
        q for q in go.quotes
        if q.player == "Drake Maye" and q.market == "player_pass_yds" and q.book == "draftkings"
    ]
    assert {(q.outcome, q.line, q.price) for q in maye_yds} == {
        ("over", 245.5, -112), ("under", 245.5, -108)
    }
    # Cross-book: fanduel hangs a different number on the same player.
    fd = [q for q in go.quotes if q.book == "fanduel" and q.market == "player_pass_yds"]
    assert {q.line for q in fd} == {244.5}


def test_off_shape_rows_are_skipped_not_fatal():
    # The fixture deliberately contains an "Anytime TD" outcome (not O/U) and
    # an h2h market inside the props response; both must be skipped silently
    # rather than crash or pollute the ladders.
    (go,) = provider().fetch_player_props(["player_receptions"])
    receptions = [q for q in go.quotes if q.market == "player_receptions"]
    assert {(q.outcome, q.price) for q in receptions} == {("over", -120), ("under", 100)}
    assert all(q.market != "h2h" for q in go.quotes)


def test_prop_markets_are_gated_per_sport():
    with_nfl = provider()
    try:
        with_nfl.fetch_player_props(["batter_home_runs"])
        raise AssertionError("expected ProviderError")
    except ProviderError as exc:
        assert "nfl" in str(exc) and "batter_home_runs" in str(exc)

    mlb = TheOddsAPI(api_key="k", sport="mlb", transport=nfl_props_transport())
    try:
        mlb.fetch_player_props(["player_pass_yds"])
        raise AssertionError("expected ProviderError")
    except ProviderError as exc:
        assert "mlb" in str(exc)


def test_nfl_prop_rows_roundtrip_storage_and_stay_off_boards(tmp_path):
    (go,) = provider().fetch_player_props(["player_pass_yds"])
    storage = Storage(tmp_path / "nfl-odds.sqlite")
    try:
        storage.store([go])
        rows = storage.history_rows(go.game.game_id)
        players = {r[7] for r in rows}
        assert "Drake Maye" in players
        # Prop ladders never appear on the latest-odds board (D-018).
        assert storage.latest_odds() == []
    finally:
        storage.close()


def test_cli_rejects_wrong_sport_market_before_spending_credits():
    result = runner.invoke(
        app, ["props", "--market", "batter_hits", "--sport", "nfl"], env={}
    )
    assert result.exit_code == 2
    assert "player_pass_yds" in result.output  # tells you the right menu
