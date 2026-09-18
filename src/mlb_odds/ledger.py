"""The NFL model's per-game read and its accuracy ledger (D-044).

Two things live here because they must agree:

1. `read_game` — the one composition of a game's market and model numbers
   (spread consensus, power-rating line, devigged moneyline consensus with
   spread-implied fallback, the D-036 two-lens blend). The contest Board, the
   survivor Matrix, and the snapshots below all call it, so no two surfaces
   can ever disagree on a number.

2. Snapshots + scoring. A snapshot is what the model said about an upcoming
   game at a poll; the ledger scores the latest PRE-KICKOFF snapshot per
   finished game. That discipline matters: the lenses are refit on every
   request over every stored line, so a fit taken today already "knows"
   last week's closing numbers — only a forecast recorded before kickoff is
   evidence. Nothing is backfilled; the ledger starts at the first poll after
   deploy and grows a week at a time.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from mlb_odds import contest, model, survivor, valuation
from mlb_odds.contest import grade_pick
from mlb_odds.models import Game, ModelSnapshot
from mlb_odds.storage import Storage

SNAPSHOT_HORIZON = timedelta(days=14)  # the current week and the look-ahead one


@dataclass(frozen=True)
class MarketFit:
    """The two season-wide fits every NFL probability composes from:
    spread-implied point ratings (D-025) and moneyline-implied strengths
    (D-036). Fit once per request, read per game."""

    ratings: dict[str, float]
    hfa: float
    ml_strengths: dict[str, float]
    ml_hfa: float | None


def fit_market(odds: Storage) -> MarketFit:
    fitted = contest.power_ratings(odds)
    fitted_ml = valuation.implied_strengths(odds)
    ratings, hfa = fitted if fitted else ({}, 0.0)
    ml_strengths, ml_hfa = fitted_ml if fitted_ml else ({}, None)
    return MarketFit(ratings=ratings, hfa=hfa, ml_strengths=ml_strengths, ml_hfa=ml_hfa)


@dataclass(frozen=True)
class GameRead:
    """One game's read, home side throughout."""

    consensus: float | None  # market home spread (median across books)
    model_line: float | None  # power-rating home spread
    home_wp: float | None  # market: devigged ML consensus, else spread-implied
    model_wp: float | None  # D-036 two-lens blend
    ml_lens: float | None
    spread_lens: float | None


def read_game(odds: Storage, game: Game, fit: MarketFit) -> GameRead:
    """The per-game math shared by every NFL surface. Must run while `odds`
    is still open."""
    # As-of kickoff on both markets: in-play quotes stored by a post-kickoff
    # poll never leak into a game's read (D-047).
    market = contest.consensus(
        contest.pregame_spreads(contest.spread_history(odds, game.game_id), game)
    )
    model_line = contest.predicted_home_spread(fit.ratings, fit.hfa, game.home_team, game.away_team)
    ml_consensus = valuation.consensus_prob(
        valuation.book_probs(valuation.moneyline_history(odds, game.game_id), asof=game.start_time)
    )
    reference = market if market is not None else model_line
    # Spread-implied fallback: survivor's straight-up conversion (ties fold
    # into the loss side, 3-dp), unchanged from the Board's original math.
    home_wp = (
        ml_consensus
        if ml_consensus is not None
        else survivor.win_probability(reference)
        if reference is not None
        else None
    )
    ml_lens = (
        valuation.model_home_prob(fit.ml_strengths, fit.ml_hfa, game.home_team, game.away_team)
        if fit.ml_hfa is not None
        else None
    )
    model_wp, spread_lens = model.nfl_model_prob(ml_lens, model_line)
    return GameRead(
        consensus=market,
        model_line=model_line,
        home_wp=home_wp,
        model_wp=model_wp,
        ml_lens=ml_lens,
        spread_lens=spread_lens,
    )


# --- snapshots ---------------------------------------------------------------------


def compute_snapshots(odds: Storage, *, now: datetime) -> list[ModelSnapshot]:
    """What the model says right now about every game kicking off within the
    horizon — the rows a poll records."""
    games = odds.games(window=(now, now + SNAPSHOT_HORIZON))
    if not games:
        return []
    fit = fit_market(odds)
    out = []
    for game in games:
        r = read_game(odds, game, fit)
        out.append(
            ModelSnapshot(
                game_id=game.game_id,
                computed_at=now,
                market_prob=r.home_wp,
                ml_lens_prob=r.ml_lens,
                spread_lens_prob=r.spread_lens,
                model_prob=r.model_wp,
                predicted_margin=round(-r.model_line, 1) if r.model_line is not None else None,
                consensus_spread=r.consensus,
            )
        )
    return out


def record_snapshots(odds: Storage, *, now: datetime) -> int:
    """Compute and append; returns rows written."""
    return odds.store_model_snapshots(compute_snapshots(odds, now=now))


# --- scoring ---------------------------------------------------------------------


def brier(outcomes: Sequence[tuple[float, int]]) -> float | None:
    """Mean Brier over (predicted home prob, home_won). 0.25 = coin flip."""
    if not outcomes:
        return None
    return round(sum((p - won) ** 2 for p, won in outcomes) / len(outcomes), 4)


LENSES = ("market", "moneyline", "spread", "model")


def accuracy(odds: Storage) -> dict[str, object]:
    """Score every finished game's latest pre-kickoff snapshot.

    straight_up: Brier + hit rate per lens (market consensus is the baseline
    the model must beat). against_spread: the side the model favored versus
    the closing-at-snapshot consensus spread, graded ATS — the number to
    weight model edges by."""
    rows = odds.model_outcomes()
    per_lens: dict[str, list[tuple[float, int]]] = {lens: [] for lens in LENSES}
    ats = {"win": 0, "loss": 0, "push": 0}
    for _gid, snap, home_score, away_score in rows:
        home_won = 1 if home_score > away_score else 0
        for lens, prob in (
            ("market", snap.market_prob),
            ("moneyline", snap.ml_lens_prob),
            ("spread", snap.spread_lens_prob),
            ("model", snap.model_prob),
        ):
            if prob is not None:
                per_lens[lens].append((prob, home_won))
        if snap.predicted_margin is not None and snap.consensus_spread is not None:
            # The market's implied home margin is -spread; the model's is
            # predicted_margin. Where they differ, the model favors one side.
            gap = snap.predicted_margin + snap.consensus_spread
            if gap != 0:
                side = "home" if gap > 0 else "away"
                ats[grade_pick(side, snap.consensus_spread, home_score, away_score)] += 1

    def summary(scored: list[tuple[float, int]]) -> dict[str, object]:
        hits = sum(1 for p, won in scored if (p >= 0.5) == (won == 1))
        return {
            "n": len(scored),
            "brier": brier(scored),
            "hit_rate": round(hits / len(scored), 3) if scored else None,
        }

    decided = ats["win"] + ats["loss"]
    return {
        "n": len(rows),
        "straight_up": {lens: summary(scored) for lens, scored in per_lens.items()},
        "against_spread": {
            "n": sum(ats.values()),
            "model_side_record": f"{ats['win']}-{ats['loss']}-{ats['push']}",
            "model_side_cover_rate": round(ats["win"] / decided, 3) if decided else None,
        },
    }
