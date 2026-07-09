"""Provider protocol. All source-specific mess stays behind this boundary."""

from typing import Protocol, runtime_checkable

from mlb_odds.models import GameOdds


class ProviderError(Exception):
    """Unrecoverable provider failure (auth, quota exhausted, repeated 5xx...).

    The collector catches this, logs it, and continues with other providers /
    the next cycle.
    """


@runtime_checkable
class OddsProvider(Protocol):
    name: str

    def fetch_game_lines(self) -> list[GameOdds]:
        """Fetch current MLB game lines for all upcoming/live games.

        Returns fully normalized models: canonical team codes, UTC times,
        American prices, one fetched_at per call. Raises ProviderError on
        unrecoverable failure.
        """
        ...
