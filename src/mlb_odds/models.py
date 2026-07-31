"""Domain models. Canonical forms: UTC times, canonical team codes, American prices."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

GAME_MARKETS = ("moneyline", "run_line", "total")
# Curated player-prop markets (The Odds API keys, D-018). Extend deliberately:
# each addition multiplies per-event credit cost.
PROP_MARKETS = ("batter_home_runs", "batter_hits", "batter_total_bases", "pitcher_strikeouts")

Market = Literal[
    "moneyline", "run_line", "total", "spread",
    "batter_home_runs", "batter_hits", "batter_total_bases", "pitcher_strikeouts",
]
Outcome = Literal["home", "away", "over", "under"]
Sport = Literal["mlb", "nfl"]


def _require_utc(v: datetime) -> datetime:
    """Reject naive datetimes and normalize aware ones to UTC.

    Storage sorts and date-filters ISO-8601 strings lexically, which is only
    correct when every stored timestamp carries the same (UTC) offset.
    """
    if v.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return v.astimezone(UTC)


def make_game_id(date_utc: str, away: str, home: str, game_number: int = 1) -> str:
    """Canonical game identity, e.g. "2026-07-09-NYM-NYY-1".

    game_number disambiguates doubleheaders (1-based, ordered by start time).
    """
    return f"{date_utc}-{away}-{home}-{game_number}"


class Game(BaseModel):
    game_id: str
    start_time: datetime
    home_team: str
    away_team: str
    provider_ids: dict[str, str] = Field(default_factory=dict)

    _normalize_start = field_validator("start_time")(_require_utc)

    @property
    def season(self) -> int:
        return self.start_time.year


class Quote(BaseModel):
    book: str
    market: Market
    outcome: Outcome
    line: float | None = None
    price: int
    player: str | None = None  # prop markets only; None for game markets

    @field_validator("price")
    @classmethod
    def _valid_american(cls, v: int) -> int:
        if -100 < v < 100:
            raise ValueError(f"American odds must be <= -100 or >= 100, got {v}")
        return v

    @model_validator(mode="after")
    def _player_matches_market(self) -> "Quote":
        if self.market in PROP_MARKETS:
            if not self.player:
                raise ValueError(f"prop market {self.market!r} requires a player")
            if self.outcome not in ("over", "under"):
                raise ValueError(f"prop market {self.market!r} outcome must be over/under")
            if self.line is None:
                raise ValueError(f"prop market {self.market!r} requires a line")
        elif self.player is not None:
            raise ValueError(f"game market {self.market!r} cannot carry a player")
        return self

    @property
    def price_decimal(self) -> float:
        if self.price > 0:
            return 1.0 + self.price / 100.0
        return 1.0 + 100.0 / -self.price


class GameOdds(BaseModel):
    game: Game
    fetched_at: datetime
    provider: str
    quotes: list[Quote]

    _normalize_fetched = field_validator("fetched_at")(_require_utc)
