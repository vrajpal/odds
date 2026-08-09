"""Moneyline valuation: devigged probabilities, market-implied team strength,
and per-price EV (D-030). Built for the MLB dashboard, sport-agnostic math.

MLB's core market is the moneyline (run lines are almost always ±1.5 with only
the price varying), so where the NFL model works in points, this module works
in probability space:

- a book's moneyline pair devigs to an implied home win probability;
- the market consensus is the median devigged probability across books;
- team strengths are fit on the log-odds scale — one equation per game,
  logit(p_home) = s_home - s_away + hfa — the exact analog of the NFL
  spread fit (D-025), ridge-regularized and mean-centered;
- a price's EV is measured against the consensus fair probability:
  EV = p_fair x decimal - 1. Positive EV = the book is offering a better
  price than the market's own average opinion says is fair.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from statistics import median

from mlb_odds.storage import Storage


def american_to_prob(price: int) -> float:
    """Implied probability of one American price (vig included)."""
    if price > 0:
        return 100.0 / (price + 100.0)
    return -price / (-price + 100.0)


def american_to_decimal(price: int) -> float:
    if price > 0:
        return 1.0 + price / 100.0
    return 1.0 + 100.0 / -price


def devig_pair(home_price: int, away_price: int) -> float:
    """Home win probability with the vig removed (multiplicative method):
    normalize the two implied probabilities to sum to 1."""
    p_home = american_to_prob(home_price)
    p_away = american_to_prob(away_price)
    return p_home / (p_home + p_away)


def expected_value(fair_prob: float, price: int) -> float:
    """EV per unit staked at `price` if `fair_prob` is the true win chance."""
    return round(fair_prob * american_to_decimal(price) - 1.0, 4)


@dataclass(frozen=True)
class MoneylineTick:
    """One book's complete moneyline pair at one snapshot."""

    fetched_at: datetime
    book: str
    home_price: int
    away_price: int

    @property
    def home_prob(self) -> float:
        return devig_pair(self.home_price, self.away_price)


def moneyline_history(odds: Storage, game_id: str) -> list[MoneylineTick]:
    """Every snapshot where a book quoted BOTH sides, oldest first.

    Pairs are matched per (fetched_at, provider, book) — a half-quoted pair
    devigs to garbage, so incomplete snapshots are dropped.
    """
    sides: dict[tuple[str, str, str], dict[str, int]] = {}
    for fetched_at, provider, book, market, outcome, _line, price, player in (
        odds.history_rows(game_id)
    ):
        if market != "moneyline" or player is not None:
            continue
        sides.setdefault((fetched_at, provider, book), {})[outcome] = price
    ticks = [
        MoneylineTick(
            fetched_at=datetime.fromisoformat(t),
            book=book,
            home_price=pair["home"],
            away_price=pair["away"],
        )
        for (t, _provider, book), pair in sides.items()
        if "home" in pair and "away" in pair
    ]
    ticks.sort(key=lambda x: (x.fetched_at, x.book))
    return ticks


def book_probs(
    ticks: list[MoneylineTick], asof: datetime | None = None
) -> dict[str, MoneylineTick]:
    """Newest complete pair per book at-or-before `asof` (None = latest).
    Carry-forward semantics match the NFL spread math (D-020)."""
    newest: dict[str, MoneylineTick] = {}
    for tick in ticks:  # oldest first: later assignment = newer wins
        if asof is not None and tick.fetched_at > asof:
            continue
        current = newest.get(tick.book)
        if current is None or tick.fetched_at >= current.fetched_at:
            newest[tick.book] = tick
    return newest


def consensus_prob(pairs: dict[str, MoneylineTick]) -> float | None:
    """Median devigged home probability across books — the fair line the EV
    column measures every offered price against."""
    if not pairs:
        return None
    return round(float(median(t.home_prob for t in pairs.values())), 4)


def implied_strengths(odds: Storage) -> tuple[dict[str, float], float] | None:
    """Market-implied team strengths (log-odds) + home advantage.

    One equation per stored game with a devigged consensus:
    logit(p_home) = s_home - s_away + hfa. Same ridge least-squares shape as
    the NFL point-ratings fit (D-025); strengths are mean-centered. Uses the
    closing consensus for started games, latest otherwise.
    """
    import numpy as np

    rows: list[tuple[str, str, float]] = []
    for game in odds.games():
        ticks = moneyline_history(odds, game.game_id)
        pairs = book_probs(ticks, asof=game.start_time) or book_probs(ticks)
        p = consensus_prob(pairs)
        if p is not None and 0.02 < p < 0.98:
            rows.append((game.home_team, game.away_team, math.log(p / (1 - p))))
    if len(rows) < 8:
        return None
    teams_sorted = sorted({t for h, a, _ in rows for t in (h, a)})
    index = {t: i for i, t in enumerate(teams_sorted)}
    n = len(teams_sorted)
    x = np.zeros((len(rows), n + 1))
    y = np.zeros(len(rows))
    for i, (home, away, logit) in enumerate(rows):
        x[i, index[home]] = 1.0
        x[i, index[away]] = -1.0
        x[i, n] = 1.0
        y[i] = logit
    lam = 1.0
    a = x.T @ x + lam * np.eye(n + 1)
    solution = np.linalg.solve(a, x.T @ y)
    strengths_raw = solution[:n] - solution[:n].mean()
    strengths = {t: round(float(s), 4) for t, s in zip(teams_sorted, strengths_raw, strict=True)}
    return strengths, round(float(solution[n]), 4)


def model_home_prob(
    strengths: dict[str, float], hfa: float, home: str, away: str
) -> float | None:
    """The model's home win probability for a matchup."""
    if home not in strengths or away not in strengths:
        return None
    logit = strengths[home] - strengths[away] + hfa
    return round(1.0 / (1.0 + math.exp(-logit)), 4)


@dataclass(frozen=True)
class SidePrice:
    """Best available price on one side, with its EV vs the consensus fair."""

    book: str
    price: int
    ev: float


def best_prices(
    pairs: dict[str, MoneylineTick], fair_home: float
) -> tuple[SidePrice | None, SidePrice | None]:
    """(home, away) best offered price by EV against the fair probability."""
    home = away = None
    for book, tick in sorted(pairs.items()):
        h = SidePrice(
            book=book, price=tick.home_price, ev=expected_value(fair_home, tick.home_price)
        )
        a = SidePrice(
            book=book, price=tick.away_price, ev=expected_value(1.0 - fair_home, tick.away_price)
        )
        if home is None or h.ev > home.ev:
            home = h
        if away is None or a.ev > away.ev:
            away = a
    return home, away
