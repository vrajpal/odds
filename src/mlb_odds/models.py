"""Domain models. Canonical forms: UTC times, canonical team codes, American prices."""

from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

GAME_MARKETS = ("moneyline", "run_line", "total", "spread")
# Curated player-prop markets (The Odds API keys, D-018/D-022), per sport.
# Extend deliberately: each addition multiplies per-event credit cost. All
# curated markets are over/under ladders (name=Over/Under, description=player,
# point=line) so one parser serves both sports.
MLB_PROP_MARKETS = ("batter_home_runs", "batter_hits", "batter_total_bases", "pitcher_strikeouts")
NFL_PROP_MARKETS = ("player_pass_yds", "player_pass_tds", "player_rush_yds", "player_receptions")
PROP_MARKETS_BY_SPORT: dict[str, tuple[str, ...]] = {
    "mlb": MLB_PROP_MARKETS,
    "nfl": NFL_PROP_MARKETS,
}
PROP_MARKETS = MLB_PROP_MARKETS + NFL_PROP_MARKETS

Market = Literal[
    "moneyline", "run_line", "total", "spread",
    "batter_home_runs", "batter_hits", "batter_total_bases", "pitcher_strikeouts",
    "player_pass_yds", "player_pass_tds", "player_rush_yds", "player_receptions",
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


class ModelSnapshot(BaseModel):
    """One pre-kickoff read of the model for a game (D-044): what every lens
    said at `computed_at`, so the accuracy ledger scores forecasts that were
    actually made, never fits that already knew the closing line."""

    game_id: str
    computed_at: datetime  # UTC, tz-aware
    market_prob: float | None  # devigged consensus (or spread-implied), home side
    ml_lens_prob: float | None
    spread_lens_prob: float | None
    model_prob: float | None  # the blend
    predicted_margin: float | None  # model home margin, points
    consensus_spread: float | None  # market home spread at the time


class NflHistoryGame(BaseModel):
    """One historical NFL game from nflverse (D-046): the closing spread and
    total, the result, and the situational context the closing-line model
    learns from. Lines are in the package convention (home spread, negative
    = home favored) — nflverse's positive-means-home-favored sign is flipped
    on import."""

    nflverse_id: str  # e.g. 2025_01_DAL_PHI
    season: int
    week: int
    game_type: str  # REG | POST | ...
    gameday: date
    away_team: str
    home_team: str
    away_score: int | None
    home_score: int | None
    spread_line: float | None  # closing home spread
    total_line: float | None  # closing total
    away_moneyline: int | None
    home_moneyline: int | None
    away_rest: int | None
    home_rest: int | None
    div_game: bool
    roof: str | None
    surface: str | None
    temp: int | None
    wind: int | None
    away_qb: str | None
    home_qb: str | None


class ClosePredictionRow(BaseModel):
    """What the closing-line model said at `computed_at` (D-046)."""

    game_id: str
    market: str  # spread | total
    computed_at: datetime
    hours_to_kick: float
    reference: str  # pinnacle | consensus — what `current` is
    current: float
    predicted_close: float
    sd: float | None
    direction: str  # home | away | over | under | flat
    p_toward: float | None
    model_version: str
