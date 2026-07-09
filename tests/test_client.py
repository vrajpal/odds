"""OddsClient tests: pluggability via FakeProvider, outage survival, DataFrame views."""

from datetime import UTC, datetime

import httpx
import pandas as pd
import pytest

from conftest import FakeProvider, make_game_odds
from mlb_odds import OddsClient
from mlb_odds.providers import TheOddsAPI
from mlb_odds.providers.base import ProviderError


@pytest.fixture
def client(tmp_path):
    c = OddsClient(providers=[FakeProvider()], db=tmp_path / "test.sqlite")
    yield c
    c.close()


def test_fetch_and_store_persists(client):
    results = client.fetch_and_store()

    assert len(results) == 1
    assert client.last_errors == {}
    stored = client.current_odds()
    assert stored == results


def test_history_df_shape(client):
    early = make_game_odds(fetched_at=datetime(2026, 7, 9, 15, 0, tzinfo=UTC))
    late = make_game_odds(fetched_at=datetime(2026, 7, 9, 16, 0, tzinfo=UTC))
    game_id = early.game.game_id
    client._storage.store([early])
    client._storage.store([late])

    df = client.history_df(game_id)
    assert list(df.columns) == [
        "fetched_at", "provider", "book", "market", "outcome", "line", "price",
    ]
    assert len(df) == len(early.quotes) + len(late.quotes)
    assert pd.api.types.is_datetime64_any_dtype(df["fetched_at"])
    assert df["fetched_at"].nunique() == 2
    # one row per (fetched_at, book, market, outcome)
    assert not df.duplicated(subset=["fetched_at", "provider", "book", "market", "outcome"]).any()


def test_history_df_empty_for_unknown_game(client):
    df = client.history_df("2026-07-09-XXX-YYY-1")
    assert df.empty
    assert list(df.columns) == [
        "fetched_at", "provider", "book", "market", "outcome", "line", "price",
    ]


def test_odds_df_shape(client):
    client.fetch_and_store()

    df = client.odds_df()
    assert list(df.columns) == [
        "game_id", "start_time", "away_team", "home_team",
        "fetched_at", "provider", "book", "market", "outcome", "line", "price",
    ]
    assert len(df) == 6
    assert pd.api.types.is_datetime64_any_dtype(df["fetched_at"])
    assert pd.api.types.is_datetime64_any_dtype(df["start_time"])
    assert set(df["market"]) == {"moneyline", "run_line", "total"}


def test_outage_survival_one_provider_down(tmp_path):
    broken = FakeProvider(name="broken", error=ProviderError("simulated outage"))
    working = FakeProvider([make_game_odds(provider="working")], name="working")
    client = OddsClient(providers=[broken, working], db=tmp_path / "test.sqlite")

    results = client.fetch_and_store()  # must not raise

    assert len(results) == 1
    assert results[0].provider == "working"
    assert list(client.last_errors) == ["broken"]
    assert isinstance(client.last_errors["broken"], ProviderError)
    stored = client.current_odds()
    assert len(stored) == 1
    assert stored[0].provider == "working"
    client.close()


def test_fetch_and_store_survives_raw_network_error(tmp_path):
    """A DNS failure / refused connection from the real provider must surface as
    ProviderError and be swallowed by the cycle, not crash it (SPEC FR1)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed")

    provider = TheOddsAPI(api_key="test-key", transport=httpx.MockTransport(handler))
    client = OddsClient(providers=[provider], db=tmp_path / "test.sqlite")

    results = client.fetch_and_store()  # must not raise

    assert results == []
    assert list(client.last_errors) == ["the_odds_api"]
    assert isinstance(client.last_errors["the_odds_api"], ProviderError)
    client.close()


def test_last_errors_reset_between_cycles(tmp_path):
    flaky = FakeProvider(name="flaky", error=ProviderError("boom"))
    client = OddsClient(providers=[flaky], db=tmp_path / "test.sqlite")

    client.fetch_and_store()
    assert "flaky" in client.last_errors

    flaky._error = None  # provider recovers
    client.fetch_and_store()
    assert client.last_errors == {}
    client.close()
