"""The closing-line model (D-046): where will the NFL number close?

Not an outcome model. The target is the pre-kickoff Pinnacle number (the
consensus at kickoff when Pinnacle has none) — a bet is judged by the line
it beat, so the question worth modelling is where the line is going.

Phase 1 (this file's first version) is the foundation: the feature read
per (game, market, instant), the training-set builder over this season's
snapshot histories, the residual scale of "close minus now" by horizon,
and a `close-v0` predictor that says the line closes where it is, with
that scale as its sd. v0 is deliberately naive — it is the baseline every
later version must beat on the grading report, and it lets consumers wire
the contract now. Predictions are recorded per poll and graded only from
rows made before kickoff (the D-044 discipline).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median, pstdev

from mlb_odds import contest
from mlb_odds.models import ClosePredictionRow, Game
from mlb_odds.storage import Storage

MODEL_VERSION = "close-v0"
MARKETS = ("spread", "total")
SHARP_BOOKS = frozenset({"pinnacle", "lowvig", "betonlineag"})
SQUARE_BOOKS = frozenset({"draftkings", "fanduel", "betmgm", "betrivers"})
HORIZONS: tuple[tuple[str, float, float], ...] = (
    ("0-24h", 0.0, 24.0),
    ("24-72h", 24.0, 72.0),
    ("72-168h", 72.0, 168.0),
    ("168h+", 168.0, float("inf")),
)
PREDICTION_HORIZON = timedelta(days=14)


@dataclass(frozen=True)
class LineTick:
    fetched_at: datetime
    book: str
    line: float


def line_history(odds: Storage, game_id: str, market: str) -> list[LineTick]:
    """Every stored home-spread or total line for a game, oldest first."""
    outcome = "home" if market == "spread" else "over"
    return [
        LineTick(fetched_at=datetime.fromisoformat(fetched_at), book=book, line=line)
        for fetched_at, _provider, book, mk, oc, line, _price, player in odds.history_rows(game_id)
        if mk == market and oc == outcome and line is not None and player is None
    ]


def book_lines_asof(ticks: Sequence[LineTick], asof: datetime | None = None) -> dict[str, float]:
    """Newest line per book at-or-before `asof`; carry-forward, like D-020."""
    out: dict[str, float] = {}
    for t in ticks:  # oldest first
        if asof is None or t.fetched_at <= asof:
            out[t.book] = t.line
    return out


def consensus_asof(ticks: Sequence[LineTick], asof: datetime | None = None) -> float | None:
    lines = book_lines_asof(ticks, asof)
    return float(median(lines.values())) if lines else None


def reference_asof(
    ticks: Sequence[LineTick], asof: datetime | None = None
) -> tuple[str, float] | None:
    """(reference name, number): Pinnacle's when it quotes, else consensus."""
    lines = book_lines_asof(ticks, asof)
    if "pinnacle" in lines:
        return "pinnacle", lines["pinnacle"]
    if lines:
        return "consensus", float(median(lines.values()))
    return None


def horizon_label(hours: float) -> str:
    for label, lo, hi in HORIZONS:
        if lo <= hours < hi:
            return label
    return HORIZONS[-1][0]


@dataclass(frozen=True)
class Features:
    """One (game, market, instant) read — the model's inputs."""

    reference: str
    current: float
    consensus: float
    opener: float
    move_open_to_now: float  # consensus now - first stored consensus
    hours_to_kick: float
    sharp_gap: float | None  # median sharp book - median square book
    velocity_24h: float | None  # consensus now - consensus 24h ago
    ratings_line: float | None  # spread only: power-rating home spread
    rest_differential: int | None
    divisional: bool


def features_asof(
    ticks: Sequence[LineTick],
    game: Game,
    asof: datetime,
    *,
    ratings_line: float | None,
    context: contest.GameContext,
) -> Features | None:
    ref = reference_asof(ticks, asof)
    consensus = consensus_asof(ticks, asof)
    if ref is None or consensus is None:
        return None
    first = next((t for t in ticks if t.fetched_at <= asof), None)
    opener = consensus_asof(ticks, first.fetched_at) if first else consensus
    lines = book_lines_asof(ticks, asof)
    sharp = [v for b, v in lines.items() if b in SHARP_BOOKS]
    square = [v for b, v in lines.items() if b in SQUARE_BOOKS]
    earlier = consensus_asof(ticks, asof - timedelta(hours=24))
    return Features(
        reference=ref[0],
        current=ref[1],
        consensus=consensus,
        opener=opener if opener is not None else consensus,
        move_open_to_now=round(consensus - (opener if opener is not None else consensus), 2),
        hours_to_kick=round((game.start_time - asof).total_seconds() / 3600.0, 2),
        sharp_gap=round(float(median(sharp)) - float(median(square)), 2)
        if sharp and square
        else None,
        velocity_24h=round(consensus - earlier, 2) if earlier is not None else None,
        ratings_line=ratings_line,
        rest_differential=context.rest_differential,
        divisional=context.divisional,
    )


def closing_number(ticks: Sequence[LineTick], kickoff: datetime) -> tuple[str, float] | None:
    """The target: Pinnacle's last pre-kickoff number, else the consensus."""
    return reference_asof(ticks, kickoff)


# --- training set ------------------------------------------------------------------


@dataclass(frozen=True)
class TrainingRow:
    game_id: str
    market: str
    asof: datetime
    features: Features
    close: float
    remaining_move: float  # close - current: the target


def training_rows(odds: Storage, *, now: datetime) -> list[TrainingRow]:
    """One row per stored snapshot of every game that has kicked off, for
    both markets — the "remaining move" dataset from this season's polls."""
    games = [g for g in odds.games() if g.start_time <= now]
    all_games = odds.games()
    fitted = contest.power_ratings(odds)
    rows: list[TrainingRow] = []
    for game in games:
        context = contest.game_context(all_games, game)
        ratings_line = (
            contest.predicted_home_spread(fitted[0], fitted[1], game.home_team, game.away_team)
            if fitted
            else None
        )
        for market in MARKETS:
            ticks = line_history(odds, game.game_id, market)
            close = closing_number(ticks, game.start_time)
            if close is None:
                continue
            for asof in sorted({t.fetched_at for t in ticks if t.fetched_at <= game.start_time}):
                feats = features_asof(
                    ticks,
                    game,
                    asof,
                    ratings_line=ratings_line if market == "spread" else None,
                    context=context,
                )
                if feats is None:
                    continue
                rows.append(
                    TrainingRow(
                        game_id=game.game_id,
                        market=market,
                        asof=asof,
                        features=feats,
                        close=close[1],
                        remaining_move=round(close[1] - feats.current, 2),
                    )
                )
    return rows


def residual_scale(rows: Sequence[TrainingRow]) -> dict[str, dict[str, float | None]]:
    """sd of the remaining move by market and horizon — v0's uncertainty."""
    buckets: dict[str, dict[str, list[float]]] = {
        m: {label: [] for label, _lo, _hi in HORIZONS} for m in MARKETS
    }
    for r in rows:
        buckets[r.market][horizon_label(r.features.hours_to_kick)].append(r.remaining_move)
    return {
        m: {
            label: (round(pstdev(vals), 3) if len(vals) >= 2 else None)
            for label, vals in per.items()
        }
        for m, per in buckets.items()
    }


# --- prediction ------------------------------------------------------------------------


@dataclass(frozen=True)
class Contribution:
    label: str
    points: float


@dataclass(frozen=True)
class ClosePrediction:
    market: str
    reference: str
    current: float
    predicted_close: float
    sd: float | None
    direction: str  # home | away | over | under | flat
    p_toward: float | None
    hours_to_kick: float
    as_of: datetime
    model_version: str = MODEL_VERSION
    contributions: list[Contribution] = field(default_factory=list)

    def row(self, game_id: str) -> ClosePredictionRow:
        return ClosePredictionRow(
            game_id=game_id,
            market=self.market,
            computed_at=self.as_of,
            hours_to_kick=self.hours_to_kick,
            reference=self.reference,
            current=self.current,
            predicted_close=self.predicted_close,
            sd=self.sd,
            direction=self.direction,
            p_toward=self.p_toward,
            model_version=self.model_version,
        )


class Predictor:
    """close-v0: the line closes where it is; sd from this season's
    residual scale at the same horizon. Fit once per request/poll."""

    def __init__(self, odds: Storage, *, now: datetime) -> None:
        self.scale = residual_scale(training_rows(odds, now=now))

    def predict(
        self, ticks: Sequence[LineTick], game: Game, market: str, now: datetime
    ) -> ClosePrediction | None:
        ref = reference_asof(ticks, now)
        if ref is None or game.start_time <= now:  # nothing left to close once it kicks off
            return None
        hours = round((game.start_time - now).total_seconds() / 3600.0, 2)
        return ClosePrediction(
            market=market,
            reference=ref[0],
            current=ref[1],
            predicted_close=ref[1],
            sd=self.scale[market].get(horizon_label(hours)),
            direction="flat",
            p_toward=None,
            hours_to_kick=hours,
            as_of=now,
        )


def record_predictions(odds: Storage, *, now: datetime) -> int:
    """Predict both markets for every game inside the horizon and store."""
    games = odds.games(window=(now, now + PREDICTION_HORIZON))
    if not games:
        return 0
    predictor = Predictor(odds, now=now)
    rows: list[ClosePredictionRow] = []
    for game in games:
        for market in MARKETS:
            pred = predictor.predict(line_history(odds, game.game_id, market), game, market, now)
            if pred is not None:
                rows.append(pred.row(game.game_id))
    return odds.store_close_predictions(rows)


# --- grading ---------------------------------------------------------------------------


def _direction_sign(direction: str) -> int:
    return {"home": -1, "away": 1, "under": -1, "over": 1}.get(direction, 0)


def report(odds: Storage, *, now: datetime) -> dict[str, object]:
    """MAE of predicted close vs the actual close, against the no-move
    baseline (current vs close), plus the direction hit rate where the
    model called a move — overall and by horizon, per market. Only
    predictions recorded before kickoff count, and only games that have
    kicked off by `now` grade — before kickoff a game's "closing number" is
    just its latest quote, which would grade every prediction as perfect."""
    closes: dict[tuple[str, str], float] = {}
    per: dict[str, dict[str, list[tuple[float, float, int, int]]]] = {
        m: {label: [] for label, _lo, _hi in HORIZONS} for m in MARKETS
    }
    games_seen: set[str] = set()
    for pred, kickoff in odds.close_predictions(before_kickoff=True):
        if kickoff > now:
            continue
        key = (pred.game_id, pred.market)
        if key not in closes:
            close = closing_number(line_history(odds, pred.game_id, pred.market), kickoff)
            closes[key] = close[1] if close else float("nan")
        close_value = closes[key]
        if close_value != close_value:  # NaN: no closing number stored
            continue
        games_seen.add(pred.game_id)
        actual = close_value - pred.current
        called = _direction_sign(pred.direction)
        hit = 1 if called and actual != 0 and (actual > 0) == (called > 0) else 0
        per[pred.market][horizon_label(pred.hours_to_kick)].append(
            (
                abs(pred.predicted_close - close_value),
                abs(pred.current - close_value),
                called != 0,
                hit,
            )
        )

    def summary(items: list[tuple[float, float, int, int]]) -> dict[str, object]:
        n = len(items)
        called = sum(1 for _e, _b, c, _h in items if c)
        hits = sum(h for _e, _b, _c, h in items)
        return {
            "n": n,
            "mae": round(sum(e for e, *_ in items) / n, 3) if n else None,
            "baseline_mae": round(sum(b for _e, b, *_ in items) / n, 3) if n else None,
            "direction_calls": called,
            "direction_hit_rate": round(hits / called, 3) if called else None,
        }

    out: dict[str, object] = {"n": len(games_seen), "model_version": MODEL_VERSION}
    for m in MARKETS:
        everything = [row for rows in per[m].values() for row in rows]
        out[m] = {
            **summary(everything),
            "by_horizon": {label: summary(rows) for label, rows in per[m].items()},
        }
    return out
