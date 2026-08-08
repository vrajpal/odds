"""Provider protocols. All source-specific mess stays behind this boundary."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from mlb_odds.models import GameOdds, make_game_id


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


@dataclass(frozen=True)
class FinalScore:
    """One event's score from a scoreboard, teams in canonical codes."""

    away_team: str
    home_team: str
    away_score: int
    home_score: int
    completed: bool
    start_time: datetime


@runtime_checkable
class ScoreSource(Protocol):
    """A source of final scores, one scoreboard day at a time (D-024)."""

    def fetch_final_scores(self, on: date) -> list[FinalScore]:
        """Scores for one scoreboard day, normalized like fetch_game_lines.
        Non-completed games are returned with completed=False."""
        ...


def assign_game_numbers(parsed: list[GameOdds]) -> list[GameOdds]:
    """Number same-matchup same-day games by start time so doubleheaders get
    distinct game_ids.

    A provider can only see games present in its current response (finished
    games drop out of feeds), so the numbering is provisional: storage
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
