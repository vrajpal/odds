"""ESPN provider tests against recorded scoreboard fixtures. No network.

The normal-day fixture is a trimmed live recording (2026-07-30); the
missing-market and unknown-team variants are edits of it.
"""

import copy
import json

import httpx
import pytest

from conftest import fixture_transport, load_fixture
from mlb_odds.client import OddsClient
from mlb_odds.providers import ESPN
from mlb_odds.providers.base import ProviderError


def _fetch(fixture: str, **kwargs) -> list:
    provider = ESPN(transport=fixture_transport(fixture), **kwargs)
    return provider.fetch_game_lines()


def test_normal_day_parses_and_normalizes():
    results = _fetch("espn_scoreboard_normal")

    # the fixture's third event is in progress with no odds node -> skipped
    assert len(results) == 2
    by_home = {go.game.home_team: go for go in results}
    assert set(by_home) == {"MIN", "CWS"}  # "Chicago White Sox" -> CWS

    kc_min = by_home["MIN"]
    assert kc_min.provider == "espn"
    assert kc_min.game.away_team == "KC"
    assert kc_min.game.start_time.tzinfo is not None
    assert kc_min.game.provider_ids == {"espn": "401816327"}
    assert kc_min.game.game_id.endswith("-KC-MIN-1")

    quotes = {(q.market, q.outcome): q for q in kc_min.quotes}
    assert quotes[("moneyline", "home")].price == -142
    assert quotes[("moneyline", "away")].price == 118
    assert quotes[("run_line", "home")].line == -1.5
    assert quotes[("run_line", "home")].price == 139
    assert quotes[("run_line", "away")].line == 1.5
    assert quotes[("total", "over")].line == 9.0
    assert quotes[("total", "under")].line == 9.0
    # every quote carries the book ESPN surfaced, not a hardcoded name
    assert {q.book for q in kc_min.quotes} == {"draftkings"}


def test_no_quota_to_report():
    provider = ESPN(transport=fixture_transport("espn_scoreboard_normal"))
    provider.fetch_game_lines()
    assert provider.quota_remaining is None


def test_missing_market_skips_market_not_game():
    results = _fetch("espn_scoreboard_missing_market")
    by_home = {go.game.home_team: go for go in results}

    markets = {q.market for q in by_home["MIN"].quotes}
    assert markets == {"moneyline", "run_line"}  # total edited out, game survives

    even = {(q.market, q.outcome): q for q in by_home["CWS"].quotes}
    assert even[("moneyline", "home")].price == 100  # "EVEN" -> +100


def test_unknown_team_skips_game_in_default_mode():
    results = _fetch("espn_scoreboard_unknown_team")
    assert {go.game.home_team for go in results} == {"CWS"}  # Isotopes game dropped


def test_unknown_team_raises_in_strict_mode():
    from mlb_odds.teams import TeamLookupError

    with pytest.raises(TeamLookupError):
        _fetch("espn_scoreboard_unknown_team", strict=True)


def test_doubleheader_gets_distinct_ids_ordered_by_start_time():
    payload = load_fixture("espn_scoreboard_normal")
    twin = copy.deepcopy(payload["events"][0])
    twin["id"] = "999999999"
    twin["date"] = twin["date"].replace("T17:40Z", "T23:10Z")
    payload["events"].append(twin)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))

    results = ESPN(transport=transport).fetch_game_lines()

    kc_min = sorted(
        (go for go in results if go.game.home_team == "MIN"),
        key=lambda go: go.game.start_time,
    )
    assert [go.game.game_id[-1] for go in kc_min] == ["1", "2"]


def test_server_error_retries_then_raises():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    with pytest.raises(ProviderError, match="server error"):
        ESPN(transport=httpx.MockTransport(handler)).fetch_game_lines()
    assert calls == 2


def test_server_error_recovers_on_retry():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=load_fixture("espn_scoreboard_normal"))

    results = ESPN(transport=httpx.MockTransport(handler)).fetch_game_lines()
    assert len(results) == 2


def test_transport_error_wrapped_after_retry():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(ProviderError, match="after retry"):
        ESPN(transport=httpx.MockTransport(handler)).fetch_game_lines()


def test_invalid_json_wrapped_in_provider_error():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(ProviderError, match="invalid JSON"):
        ESPN(transport=transport).fetch_game_lines()


def test_unexpected_shape_raises():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=[1, 2]))
    with pytest.raises(ProviderError, match="unexpected response shape"):
        ESPN(transport=transport).fetch_game_lines()


def test_plugs_into_client_and_stores(tmp_path):
    """SPEC pluggability, with a real second provider: no storage/client changes
    needed for ESPN results to persist alongside the_odds_api's."""
    client = OddsClient(
        providers=[ESPN(transport=fixture_transport("espn_scoreboard_normal"))],
        db=tmp_path / "espn.sqlite",
    )
    results = client.fetch_and_store()
    assert len(results) == 2
    assert len(client.current_odds()) == 2
    assert client.last_errors == {}
    client.close()


def test_fixture_is_valid_json_with_expected_events():
    """Guard the recorded fixture itself: three events, exactly one without odds."""
    payload = json.loads(json.dumps(load_fixture("espn_scoreboard_normal")))
    assert len(payload["events"]) == 3
    without_odds = [
        e for e in payload["events"] if not e["competitions"][0].get("odds")
    ]
    assert len(without_odds) == 1
