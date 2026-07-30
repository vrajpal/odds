"""OddsClient — the one object library users touch. Orchestrates providers + storage."""

import logging
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from mlb_odds.models import Game, GameOdds
from mlb_odds.providers.base import OddsProvider, ProviderError
from mlb_odds.storage import Storage

logger = logging.getLogger("mlb_odds.client")

HISTORY_COLUMNS = ["fetched_at", "provider", "book", "market", "outcome", "line", "price"]
ODDS_COLUMNS = [
    "game_id", "start_time", "away_team", "home_team",
    "fetched_at", "provider", "book", "market", "outcome", "line", "price",
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

    def close(self) -> None:
        self._storage.close()
