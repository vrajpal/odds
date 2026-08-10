"""FanDuel Research provider tests (D-037). Fixtures are trimmed live
recordings (labeled in-file); no network — httpx.MockTransport throughout."""

import json
from pathlib import Path

import httpx
import pytest

from mlb_odds.projections import aggregate_players
from mlb_odds.providers.base import ProviderError
from mlb_odds.providers.fanduel_research import (
    GRAPHQL_URL,
    PAGE_URL,
    FanDuelResearch,
)

FIXTURES = Path(__file__).parent / "fixtures"
PAGE_HTML = (FIXTURES / "fanduel_research_page.html").read_text()
GRAPHQL_JSON = (FIXTURES / "fanduel_research_graphql_batters.json").read_text()


def make_provider(page=PAGE_HTML, graphql=GRAPHQL_JSON, capture=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PAGE_URL:
            return httpx.Response(200, text=page)
        assert str(request.url) == GRAPHQL_URL
        if capture is not None:
            capture.append(json.loads(request.content))
        return httpx.Response(
            200, text=graphql, headers={"Content-Type": "application/json"}
        )

    return FanDuelResearch(client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_fetch_resolves_slate_and_aggregates():
    sent: list[dict] = []
    provider = make_provider(capture=sent)
    players = provider.fetch_mlb_batters()
    # "All Day" preferred over "Main" (fuller schedule), id from the page.
    assert sent[0]["variables"]["input"] == {
        "type": "DAILY", "sport": "MLB",
        "position": "MLB_BATTER", "slateId": "971990",
    }
    # FanDuel codes (CHW, OAK) landed as canonical (CWS, ATH).
    games = {(p.away_team, p.home_team) for p in players}
    assert games == {("LAA", "MIA"), ("CLE", "CWS"), ("ATH", "BOS")}
    rows = aggregate_players(players)
    assert len(rows) == 3
    assert all(r.home_win_prob is None and r.home_score is not None for r in rows)


def test_fetch_falls_back_to_main_slate():
    sent: list[dict] = []
    page = PAGE_HTML.replace('"label": "All Day"', '"label": "All Day Gone"') \
        .replace('"label":"All Day"', '"label":"All Day Gone"')
    provider = make_provider(page=page, capture=sent)
    provider.fetch_mlb_batters()
    assert sent[0]["variables"]["input"]["slateId"] == "971982"  # Main


def test_page_without_slates_or_data_blob_raises():
    with pytest.raises(ProviderError, match="no usable slate"):
        make_provider(
            page=PAGE_HTML.replace('"slatesFilter": [', '"slatesFilter": [] , "x": [')
        ).fetch_mlb_batters()
    with pytest.raises(ProviderError, match="__NEXT_DATA__"):
        make_provider(page="<html><body>maintenance</body></html>").fetch_mlb_batters()


def test_graphql_errors_and_empty_payloads_raise():
    with pytest.raises(ProviderError, match="graphql errors"):
        make_provider(
            graphql='{"errors": [{"message": "Internal error"}]}'
        ).fetch_mlb_batters()
    with pytest.raises(ProviderError, match="no usable batter rows"):
        make_provider(
            graphql='{"data": {"getProjections": []}}'
        ).fetch_mlb_batters()
