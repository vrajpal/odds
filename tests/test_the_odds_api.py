import httpx
import pytest

from conftest import fixture_transport, load_fixture
from mlb_odds.providers import ProviderError, TheOddsAPI
from mlb_odds.storage import Storage


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


def test_doubleheader_ids_stay_stable_after_game_one_drops_from_feed(tmp_path):
    """Once game 1 of a doubleheader finishes, The Odds API drops it from /odds.

    The next cycle sees game 2 alone (provisionally numbered 1); storage must
    still file it under its previously assigned game_id (FR2).
    """
    storage = Storage(tmp_path / "dh.sqlite")
    storage.store(make_provider("doubleheader", strict=True).fetch_game_lines())

    remaining = [e for e in load_fixture("doubleheader") if e["id"] == "dh-game-two-listed-first"]
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=remaining))
    provider = TheOddsAPI(api_key="test-key", transport=transport, strict=True)
    results = provider.fetch_game_lines()
    storage.store(results)

    assert results[0].game.game_id == "2026-07-09-STL-CHC-2"
    by_native = {g.provider_ids["the_odds_api"]: g for g in storage.games()}
    assert by_native["dh-game-one-listed-second"].game_id == "2026-07-09-STL-CHC-1"
    assert by_native["dh-game-two-listed-first"].game_id == "2026-07-09-STL-CHC-2"
    # game 1's start time was not clobbered by game 2's
    assert by_native["dh-game-one-listed-second"].start_time.hour == 17
    # game 1's history got no rows from the second cycle
    game1_fetches = {r[0] for r in storage.history_rows("2026-07-09-STL-CHC-1")}
    game2_fetches = {r[0] for r in storage.history_rows("2026-07-09-STL-CHC-2")}
    assert len(game1_fetches) == 1
    assert len(game2_fetches) == 2
    storage.close()


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


def test_transport_error_wrapped_in_provider_error_after_retry():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused")

    provider = TheOddsAPI(api_key="test-key", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="after retry"):
        provider.fetch_game_lines()
    assert calls == 2


def test_transport_error_recovers_on_retry():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadError("connection reset")
        return httpx.Response(200, json=[], headers={"x-requests-remaining": "400"})

    provider = TheOddsAPI(api_key="test-key", transport=httpx.MockTransport(handler))
    assert provider.fetch_game_lines() == []
    assert calls == 2


def test_invalid_json_body_wrapped_in_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>totally not json</html>")

    provider = TheOddsAPI(api_key="test-key", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="invalid JSON"):
        provider.fetch_game_lines()


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


def test_bookmakers_param_replaces_region(monkeypatch):
    """D-027: named books ride the `bookmakers` param; `regions` is dropped
    so the two never combine (which would raise the poll cost)."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(
            200, json=load_fixture("odds_api_nfl_normal"),
            headers={"x-requests-remaining": "463"},
        )

    provider = TheOddsAPI(
        api_key="k", sport="nfl",
        bookmakers=["pinnacle", "draftkings"],
        transport=httpx.MockTransport(handler),
    )
    provider.fetch_game_lines()
    assert seen["bookmakers"] == "pinnacle,draftkings"
    assert "regions" not in seen


def test_default_still_polls_us_region():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["regions"] == "us"
        assert "bookmakers" not in dict(request.url.params)
        return httpx.Response(
            200, json=load_fixture("normal_day"),
            headers={"x-requests-remaining": "463"},
        )

    TheOddsAPI(api_key="k", transport=httpx.MockTransport(handler)).fetch_game_lines()


def test_more_than_ten_bookmakers_refused():
    with pytest.raises(ProviderError, match="max 10"):
        TheOddsAPI(api_key="k", bookmakers=[f"book{i}" for i in range(11)])
