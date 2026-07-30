"""The Odds API (the-odds-api.com, v4) provider.

Quota: a game-lines request costs markets x regions = 3 credits; the free tier is
500/month (~5 polls/day). Credits remaining are read from response headers and
exposed as .quota_remaining.
"""

import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from mlb_odds import teams
from mlb_odds.models import PROP_MARKETS, Game, GameOdds, Market, Outcome, Quote, make_game_id
from mlb_odds.providers.base import ProviderError, assign_game_numbers

logger = logging.getLogger("mlb_odds.providers.the_odds_api")

BASE_URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb"
API_URL = f"{BASE_URL}/odds"
EVENTS_URL = f"{BASE_URL}/events"
_MARKET_MAP: dict[str, Market] = {"h2h": "moneyline", "spreads": "run_line", "totals": "total"}


class TheOddsAPI:
    name = "the_odds_api"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        strict: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """strict=True turns data surprises (unknown teams) into errors instead of
        logged skips — used in tests. transport is injectable for fixture-based tests.
        """
        key = api_key or os.environ.get("THE_ODDS_API_KEY")
        if not key:
            raise ProviderError("The Odds API key missing: pass api_key or set THE_ODDS_API_KEY")
        self._api_key = key
        self._strict = strict
        self._client = httpx.Client(transport=transport, timeout=10.0)
        self.quota_remaining: int | None = None

    def fetch_game_lines(self) -> list[GameOdds]:
        events = self._request()
        fetched_at = datetime.now(UTC)
        parsed: list[GameOdds] = []
        for event in events:
            game_odds = self._parse_event(event, fetched_at)
            if game_odds is not None:
                parsed.append(game_odds)
        return assign_game_numbers(parsed)

    def fetch_player_props(self, markets: Sequence[str]) -> list[GameOdds]:
        """Fetch player-prop ladders for every listed event (D-018).

        Metered differently from game lines: the events list is free, but each
        event's odds request costs [markets returned] x [regions]. With ~15
        games on a slate, one two-market sweep can cost up to ~30 credits —
        the CLI prints the worst case before spending.
        """
        unknown = [m for m in markets if m not in PROP_MARKETS]
        if unknown:
            raise ProviderError(
                f"unsupported prop market(s) {unknown}; supported: {list(PROP_MARKETS)}"
            )
        events = self._request_json(EVENTS_URL, {"apiKey": self._api_key})
        fetched_at = datetime.now(UTC)
        parsed: list[GameOdds] = []
        for event in events:
            body = self._request_json(
                f"{EVENTS_URL}/{event['id']}/odds",
                {
                    "apiKey": self._api_key,
                    "regions": "us",
                    "markets": ",".join(markets),
                    "oddsFormat": "american",
                },
                expect="dict",
            )
            game_odds = self._parse_prop_event(body, fetched_at)
            if game_odds is not None:
                parsed.append(game_odds)
        return assign_game_numbers(parsed)

    def _parse_prop_event(self, event: dict[str, Any], fetched_at: datetime) -> GameOdds | None:
        try:
            home = teams.normalize(self.name, event["home_team"])
            away = teams.normalize(self.name, event["away_team"])
        except teams.TeamLookupError as exc:
            if self._strict:
                raise
            logger.warning("skipping event %s: %s", event.get("id"), exc)
            return None
        start_time = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
        quotes: list[Quote] = []
        for bookmaker in event.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market["key"] not in PROP_MARKETS:
                    continue
                for raw in market["outcomes"]:
                    side = raw.get("name", "").lower()
                    if side not in ("over", "under") or raw.get("description") is None:
                        logger.warning(
                            "skipping prop outcome %r in %s/%s",
                            raw.get("name"),
                            bookmaker["key"],
                            market["key"],
                        )
                        continue
                    quotes.append(
                        Quote(
                            book=bookmaker["key"],
                            market=market["key"],
                            outcome=side,
                            line=raw.get("point"),
                            price=raw["price"],
                            player=raw["description"],
                        )
                    )
        if not quotes:
            return None  # finished/unlisted events return an empty bookmakers list
        game = Game(
            game_id=make_game_id(start_time.date().isoformat(), away, home),
            start_time=start_time,
            home_team=home,
            away_team=away,
            provider_ids={self.name: event["id"]},
        )
        return GameOdds(game=game, fetched_at=fetched_at, provider=self.name, quotes=quotes)

    def _request(self) -> list[dict[str, Any]]:
        params = {
            "apiKey": self._api_key,
            "regions": "us",
            "markets": "h2h,spreads,totals",
            "oddsFormat": "american",
        }
        result: list[dict[str, Any]] = self._request_json(API_URL, params)
        return result

    def _request_json(
        self, url: str, params: dict[str, str], *, expect: str = "list"
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                # Timeouts, DNS failures, refused/reset connections, protocol
                # errors... anything transport-level gets one retry; the final
                # failure is wrapped in ProviderError so the collector survives
                # (SPEC FR1: provider failures must not crash the collector).
                last_error = exc
                continue
            if response.status_code >= 500:
                last_error = ProviderError(
                    f"server error {response.status_code} (attempt {attempt + 1})"
                )
                continue
            if response.status_code != 200:
                raise ProviderError(f"request failed: {response.status_code} {response.text[:200]}")
            remaining = response.headers.get("x-requests-remaining")
            if remaining is not None:
                self.quota_remaining = int(float(remaining))
                logger.info("The Odds API credits remaining: %s", self.quota_remaining)
            try:
                payload = response.json()
            except ValueError as exc:  # json.JSONDecodeError
                raise ProviderError(f"invalid JSON in response: {exc}") from exc
            expected_type: type = list if expect == "list" else dict
            if not isinstance(payload, expected_type):
                raise ProviderError(f"unexpected response shape: {type(payload).__name__}")
            return payload
        raise ProviderError(f"request failed after retry: {last_error}") from last_error

    def _parse_event(self, event: dict[str, Any], fetched_at: datetime) -> GameOdds | None:
        try:
            home = teams.normalize(self.name, event["home_team"])
            away = teams.normalize(self.name, event["away_team"])
        except teams.TeamLookupError as exc:
            if self._strict:
                raise
            logger.warning("skipping event %s: %s", event.get("id"), exc)
            return None

        start_time = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
        raw_names = {event["home_team"]: "home", event["away_team"]: "away"}
        quotes: list[Quote] = []
        for bookmaker in event.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                market_key = _MARKET_MAP.get(market["key"])
                if market_key is None:
                    continue
                for raw in market["outcomes"]:
                    outcome = self._outcome(market_key, raw["name"], raw_names)
                    if outcome is None:
                        logger.warning(
                            "skipping outcome %r in %s/%s",
                            raw["name"],
                            bookmaker["key"],
                            market["key"],
                        )
                        continue
                    quotes.append(
                        Quote(
                            book=bookmaker["key"],
                            market=market_key,
                            outcome=outcome,
                            line=raw.get("point"),
                            price=raw["price"],
                        )
                    )

        # game_number is provisional here; _assign_game_numbers fixes doubleheaders.
        game = Game(
            game_id=make_game_id(start_time.date().isoformat(), away, home),
            start_time=start_time,
            home_team=home,
            away_team=away,
            provider_ids={self.name: event["id"]},
        )
        return GameOdds(game=game, fetched_at=fetched_at, provider=self.name, quotes=quotes)

    @staticmethod
    def _outcome(market: Market, raw_name: str, raw_names: dict[str, str]) -> Outcome | None:
        if market == "total":
            lowered = raw_name.lower()
            return lowered if lowered in ("over", "under") else None  # type: ignore[return-value]
        side = raw_names.get(raw_name)
        return side  # type: ignore[return-value]

