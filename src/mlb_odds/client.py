"""OddsClient — the one object library users touch. Orchestrates providers + storage."""

import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from mlb_odds.models import Game, GameOdds
from mlb_odds.providers.base import FinalScore, OddsProvider, ProviderError, ScoreSource
from mlb_odds.storage import SAME_GAME_START_TOLERANCE, Storage

logger = logging.getLogger("mlb_odds.client")

HISTORY_COLUMNS = [
    "fetched_at", "provider", "book", "market", "outcome", "line", "price", "player",
]
ODDS_COLUMNS = [
    "game_id", "start_time", "away_team", "home_team",
    "fetched_at", "provider", "book", "market", "outcome", "line", "price", "player",
]


class OddsClient:
    def __init__(
        self,
        providers: Sequence[OddsProvider],
        db: str | Path = "./odds.sqlite",
        *,
        read_only: bool = False,
        changed_only: bool = False,
    ) -> None:
        """`read_only=True` opens the database without creating or migrating it;
        fetch_and_store() then raises. See Storage.__init__.

        `changed_only=True` makes fetch_and_store append only quotes whose
        (line, price) differ from the newest stored row (D-015)."""
        if read_only and providers:
            raise ValueError("read_only clients cannot poll providers")
        self._providers = list(providers)
        self._storage = Storage(db, read_only=read_only)
        self._changed_only = changed_only
        self.last_errors: dict[str, ProviderError] = {}

    @property
    def providers(self) -> tuple[OddsProvider, ...]:
        return tuple(self._providers)

    def fetch_and_store(self) -> list[GameOdds]:
        """Poll every provider and persist what comes back.

        One provider failing never aborts the others: its error is logged and kept
        in .last_errors, and the cycle continues.
        """
        self.last_errors = {}
        results: list[GameOdds] = []
        for provider in self._providers:
            try:
                fetched = provider.fetch_game_lines()
            except ProviderError as exc:
                logger.error("provider %s failed: %s", provider.name, exc)
                self.last_errors[provider.name] = exc
                continue
            results.extend(fetched)
        rows = self._storage.store(results, changed_only=self._changed_only)
        logger.info(
            "cycle complete: %d games, %d rows written, %d provider error(s)",
            len(results),
            rows,
            len(self.last_errors),
        )
        return results

    def fetch_and_store_props(self, markets: Sequence[str]) -> list[GameOdds]:
        """Poll player-prop markets from every provider that supports them.

        Providers advertise support with a `fetch_player_props(markets)` method
        (currently TheOddsAPI only); others are skipped silently. Same error
        isolation as fetch_and_store. Metered: see the README's props credit
        math before putting this in a loop.
        """
        self.last_errors = {}
        results: list[GameOdds] = []
        for provider in self._providers:
            fetch = getattr(provider, "fetch_player_props", None)
            if fetch is None:
                logger.info("provider %s has no player-prop support, skipping", provider.name)
                continue
            try:
                results.extend(fetch(markets))
            except ProviderError as exc:
                logger.error("provider %s props failed: %s", provider.name, exc)
                self.last_errors[provider.name] = exc
        rows = self._storage.store(results, changed_only=self._changed_only)
        logger.info(
            "props cycle complete: %d games, %d rows written, %d provider error(s)",
            len(results),
            rows,
            len(self.last_errors),
        )
        return results

    def current_odds(
        self,
        on_date: date | None = None,
        *,
        window: tuple[datetime, datetime] | None = None,
    ) -> list[GameOdds]:
        """Latest stored quotes per (game, book, market). No network calls.

        `on_date` matches a UTC date; `window` is a half-open UTC [start, end)
        range for callers whose day boundary isn't UTC's. Narrowing is strongly
        preferred on a long-lived database — see Storage.latest_odds.
        """
        return self._storage.latest_odds(on_date, window=window)

    def games(
        self,
        on_date: date | None = None,
        *,
        window: tuple[datetime, datetime] | None = None,
    ) -> list[Game]:
        return self._storage.games(on_date, window=window)

    def closing_odds(
        self,
        on_date: date | None = None,
        *,
        window: tuple[datetime, datetime] | None = None,
    ) -> list[GameOdds]:
        """Closing lines: newest stored quotes at or before each game's
        start_time. Games with no pre-start snapshot are absent. No network."""
        return self._storage.closing_odds(on_date, window=window)

    def history_df(self, game_id: str) -> pd.DataFrame:
        """Line movement for one game: a row per (fetched_at, provider, book, market, outcome)."""
        df = pd.DataFrame(self._storage.history_rows(game_id), columns=HISTORY_COLUMNS)
        if not df.empty:
            df["fetched_at"] = pd.to_datetime(df["fetched_at"])
        return df

    def odds_df(self, on_date: date | None = None) -> pd.DataFrame:
        """Flat DataFrame of stored odds joined with game context."""
        df = pd.DataFrame(self._storage.all_rows(on_date), columns=ODDS_COLUMNS)
        if not df.empty:
            df["fetched_at"] = pd.to_datetime(df["fetched_at"])
            df["start_time"] = pd.to_datetime(df["start_time"])
        return df

    def fetch_and_store_results(
        self, source: ScoreSource, days: Iterable[date], *, before: datetime | None = None
    ) -> int:
        """Fetch finals for the given scoreboard days and record scores for
        stored games (D-024). Returns the number of games recorded.

        Matching is (away, home) plus start-time proximity — the same
        SAME_GAME_START_TOLERANCE storage uses for game identity, so an MLB
        doubleheader's halves each get their own score. A day whose fetch
        fails is logged and skipped; the others still land (collector policy).
        """
        finals: list[FinalScore] = []
        for day in sorted(set(days)):
            try:
                finals.extend(f for f in source.fetch_final_scores(day) if f.completed)
            except ProviderError as exc:
                logger.error("finals fetch failed for %s: %s", day, exc)
        if not finals:
            return 0
        now = datetime.now(UTC)
        recorded = 0
        for game in self._storage.games_missing_results(before=before or now):
            candidates = [
                f
                for f in finals
                if f.away_team == game.away_team
                and f.home_team == game.home_team
                and abs(f.start_time - game.start_time) <= SAME_GAME_START_TOLERANCE
            ]
            if not candidates:
                continue
            final = min(candidates, key=lambda f: abs(f.start_time - game.start_time))
            self._storage.record_result(
                game.game_id, final.home_score, final.away_score, fetched_at=now
            )
            recorded += 1
        logger.info("results: %d final score(s) recorded", recorded)
        return recorded

    def games_missing_results(self, *, before: datetime) -> list[Game]:
        """Stored games started before `before` with no recorded final."""
        return self._storage.games_missing_results(before=before)

    def result(self, game_id: str) -> tuple[int, int] | None:
        """(home_score, away_score) if final, else None."""
        return self._storage.result(game_id)

    def close(self) -> None:
        self._storage.close()
