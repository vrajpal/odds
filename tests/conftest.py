import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from mlb_odds.models import Game, GameOdds, Quote, make_game_id
from mlb_odds.providers.base import ProviderError

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def fixture_transport(name: str, *, quota_remaining: str = "497") -> httpx.MockTransport:
    """A transport that serves a recorded The Odds API response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=load_fixture(name),
            headers={"x-requests-remaining": quota_remaining, "x-requests-used": "3"},
        )

    return httpx.MockTransport(handler)


def make_game_odds(
    *,
    away: str = "NYM",
    home: str = "NYY",
    start_time: datetime | None = None,
    fetched_at: datetime | None = None,
    game_number: int = 1,
    provider: str = "fake",
    native_id: str | None = None,
    quotes: list[Quote] | None = None,
) -> GameOdds:
    """A canned, fully normalized GameOdds for storage/client/CLI tests."""
    start = start_time or datetime(2026, 7, 9, 23, 5, tzinfo=UTC)
    game_id = make_game_id(start.date().isoformat(), away, home, game_number)
    game = Game(
        game_id=game_id,
        start_time=start,
        home_team=home,
        away_team=away,
        provider_ids={provider: native_id or f"native-{game_id}"},
    )
    if quotes is None:
        quotes = [
            Quote(book="draftkings", market="moneyline", outcome="away", price=+120),
            Quote(book="draftkings", market="moneyline", outcome="home", price=-140),
            Quote(book="draftkings", market="run_line", outcome="away", line=1.5, price=-110),
            Quote(book="draftkings", market="run_line", outcome="home", line=-1.5, price=-105),
            Quote(book="draftkings", market="total", outcome="over", line=8.5, price=-108),
            Quote(book="draftkings", market="total", outcome="under", line=8.5, price=-112),
        ]
    return GameOdds(
        game=game,
        fetched_at=fetched_at or datetime(2026, 7, 9, 15, 0, tzinfo=UTC),
        provider=provider,
        quotes=quotes,
    )


NFL_KICKOFF = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)  # Sun of contest week 1, 10:00 PT


def make_nfl_spread_odds(
    book_lines: dict[str, float],
    fetched_at: datetime,
    *,
    away: str = "KC",
    home: str = "LAC",
    start_time: datetime | None = None,
    provider: str = "fake",
) -> GameOdds:
    """One NFL fetch snapshot: home/away spread quotes per book at `fetched_at`.

    `book_lines` maps book -> home spread (negative = home favored), the same
    convention the contest module computes edges in.
    """
    start = start_time or NFL_KICKOFF
    game_id = make_game_id(start.date().isoformat(), away, home)
    game = Game(
        game_id=game_id,
        start_time=start,
        home_team=home,
        away_team=away,
        provider_ids={provider: f"native-{game_id}"},
    )
    quotes = []
    for book, home_spread in book_lines.items():
        quotes.append(
            Quote(book=book, market="spread", outcome="home", line=home_spread, price=-110)
        )
        quotes.append(
            Quote(book=book, market="spread", outcome="away", line=-home_spread, price=-110)
        )
    return GameOdds(game=game, fetched_at=fetched_at, provider=provider, quotes=quotes)


class FakeProvider:
    """A second provider implemented purely against the OddsProvider protocol.

    Proves the SPEC pluggability criterion: no changes to storage/client/
    collector/CLI were needed for it to work. Configurable to raise
    ProviderError to simulate an outage.
    """

    def __init__(
        self,
        results: list[GameOdds] | None = None,
        *,
        name: str = "fake",
        error: ProviderError | None = None,
        quota_remaining: int | None = 42,
    ) -> None:
        self.name = name
        self._results = results if results is not None else [make_game_odds(provider=name)]
        self._error = error
        self.quota_remaining = quota_remaining
        self.calls = 0

    def fetch_game_lines(self) -> list[GameOdds]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._results)
