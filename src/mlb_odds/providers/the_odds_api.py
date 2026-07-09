"""The Odds API (the-odds-api.com, v4) provider.

Quota: a game-lines request costs markets x regions = 3 credits; the free tier is
500/month (~5 polls/day). Credits remaining are read from response headers and
exposed as .quota_remaining.
"""

import logging
import os
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import httpx

from mlb_odds import teams
from mlb_odds.models import Game, GameOdds, Market, Outcome, Quote, make_game_id
from mlb_odds.providers.base import ProviderError

logger = logging.getLogger("mlb_odds.providers.the_odds_api")

API_URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
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
        return self._assign_game_numbers(parsed)

    def _request(self) -> list[dict[str, Any]]:
        params = {
            "apiKey": self._api_key,
            "regions": "us",
            "markets": "h2h,spreads,totals",
            "oddsFormat": "american",
        }
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = self._client.get(API_URL, params=params)
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
            if not isinstance(payload, list):
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

    @staticmethod
    def _assign_game_numbers(parsed: list[GameOdds]) -> list[GameOdds]:
        """Number same-matchup same-day games by start time so doubleheaders get
        distinct game_ids.

        This can only see games present in the current response (finished games
        drop out of the feed), so the numbering is provisional: storage
        reconciles it against previously stored native ids on write
        (Storage._resolve_game_id) to keep identity stable across cycles.
        """
        groups: dict[str, list[GameOdds]] = defaultdict(list)
        for go in parsed:
            key = make_game_id(
                go.game.start_time.date().isoformat(), go.game.away_team, go.game.home_team
            )
            groups[key].append(go)
        for group in groups.values():
            if len(group) == 1:
                continue
            group.sort(key=lambda go: go.game.start_time)
            for number, go in enumerate(group, start=1):
                go.game.game_id = make_game_id(
                    go.game.start_time.date().isoformat(),
                    go.game.away_team,
                    go.game.home_team,
                    number,
                )
        return parsed
