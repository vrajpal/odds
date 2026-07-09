import httpx
import pytest

from conftest import fixture_transport
from mlb_odds.providers import ProviderError, TheOddsAPI


def make_provider(fixture: str, **kwargs) -> TheOddsAPI:
    return TheOddsAPI(api_key="test-key", transport=fixture_transport(fixture), **kwargs)


def test_normal_day_parses_and_normalizes():
    provider = make_provider("normal_day", strict=True)
    results = provider.fetch_game_lines()

    assert len(results) == 2
    by_id = {go.game.game_id: go for go in results}

    nyy = by_id["2026-07-09-NYM-NYY-1"]
    assert nyy.game.home_team == "NYY"
    assert nyy.game.away_team == "NYM"
    assert nyy.provider == "the_odds_api"
    assert nyy.game.provider_ids == {"the_odds_api": "e912aabbccdd001"}
    assert nyy.game.start_time.tzinfo is not None

    # DK: 3 markets x 2 outcomes; FD: 2 markets x 2 outcomes
    assert len(nyy.quotes) == 10
    dk_ml_home = next(
        q for q in nyy.quotes
        if q.book == "draftkings" and q.market == "moneyline" and q.outcome == "home"
    )
    assert dk_ml_home.price == -145
    assert dk_ml_home.line is None

    dk_rl_home = next(
        q for q in nyy.quotes
        if q.book == "draftkings" and q.market == "run_line" and q.outcome == "home"
    )
    assert dk_rl_home.line == -1.5
    assert dk_rl_home.price == 120

    fd_total_under = next(
        q for q in nyy.quotes
        if q.book == "fanduel" and q.market == "total" and q.outcome == "under"
    )
    assert fd_total_under.line == 8.5
    assert fd_total_under.price == -112

    # "Athletics" (no city) normalizes; late-night UTC start lands on 7/10
    ath = by_id["2026-07-10-ATH-LAD-1"]
    assert ath.game.away_team == "ATH"

    # one fetched_at per cycle
    assert nyy.fetched_at == ath.fetched_at


def test_quota_header_surfaced():
    provider = make_provider("normal_day")
    provider.fetch_game_lines()
    assert provider.quota_remaining == 497


def test_doubleheader_gets_distinct_ids_ordered_by_start_time():
    provider = make_provider("doubleheader", strict=True)
    results = provider.fetch_game_lines()

    ids = {go.game.game_id: go for go in results}
    assert set(ids) == {"2026-07-09-STL-CHC-1", "2026-07-09-STL-CHC-2"}
    # fixture lists the later game first; game 1 must be the earlier start
    assert ids["2026-07-09-STL-CHC-1"].game.provider_ids["the_odds_api"] == (
        "dh-game-one-listed-second"
    )


def test_unknown_team_skips_game_in_default_mode():
    provider = make_provider("unknown_team")
    results = provider.fetch_game_lines()
    assert len(results) == 1
    assert results[0].game.game_id == "2026-07-09-HOU-SEA-1"


def test_unknown_team_raises_in_strict_mode():
    provider = make_provider("unknown_team", strict=True)
    with pytest.raises(Exception, match="Montreal Expos"):
        provider.fetch_game_lines()


def test_unrecognized_market_ignored():
    provider = make_provider("missing_market", strict=True)
    results = provider.fetch_game_lines()
    assert len(results) == 1
    quotes = results[0].quotes
    # the "outrights" market and the empty-markets book contribute nothing
    assert {q.market for q in quotes} == {"moneyline"}
    assert len(quotes) == 2


def test_server_error_retries_then_raises():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="boom")

    provider = TheOddsAPI(api_key="test-key", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="after retry"):
        provider.fetch_game_lines()
    assert calls == 2


def test_server_error_recovers_on_retry():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=[], headers={"x-requests-remaining": "400"})

    provider = TheOddsAPI(api_key="test-key", transport=httpx.MockTransport(handler))
    assert provider.fetch_game_lines() == []
    assert calls == 2


def test_auth_error_raises_without_retry():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="invalid key")

    provider = TheOddsAPI(api_key="bad-key", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="401"):
        provider.fetch_game_lines()
    assert calls == 1


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="key missing"):
        TheOddsAPI()
