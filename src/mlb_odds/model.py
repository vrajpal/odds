"""The odds page's betting model (D-036): one uniform composition per sport.

Everything here is composition and unit conversion — the fitted inputs come
from valuation.implied_strengths (moneyline-implied, both sports),
contest.power_ratings (spread-implied points, NFL), and the Statcast term
(MLB, D-032). Sport shapes:

- NFL: two independent market lenses on the same question — what the
  moneylines imply and what the spreads imply. Spread points convert to win
  probability through the classical NFL margin distribution (sigma ~13.45:
  a 3-point favorite wins ~59%). Blend: equal weight in logit space.
- MLB: market strengths + Statcast (D-032 weights, unchanged).

Model EV: a price's expected value at the MODEL's probability rather than
the consensus fair — the "if you trust the model, is this price a bet"
number. Kept separate from the consensus EV, which is pure price shopping.
"""

from __future__ import annotations

import math

NFL_MARGIN_SIGMA = 13.45  # empirical stdev of NFL margins vs the spread
MLB_MARGIN_SIGMA = 3.9  # empirical stdev of MLB run margins

# Blend priors (D-036/D-037) — renormalized over present lenses; the
# projections accuracy ledger is the path to fitting these from evidence.
NFL_WEIGHTS = {"moneyline": 0.35, "spread": 0.35, "projection": 0.30}
MLB_WEIGHTS = {"market": 0.49, "statcast": 0.21, "projection": 0.30}


def margin_to_prob(margin: float, sigma: float = NFL_MARGIN_SIGMA) -> float:
    """P(home win) given a predicted home margin, via the normal CDF."""
    return round(0.5 * (1.0 + math.erf(margin / (sigma * math.sqrt(2.0)))), 4)


def prob_to_logit(prob: float | None) -> float | None:
    if prob is None or not 0 < prob < 1:
        return None
    return math.log(prob / (1 - prob))


def logit_to_prob(logit: float) -> float:
    return round(1.0 / (1.0 + math.exp(-logit)), 4)


def blend_logits(components: list[float | None], weights: list[float]) -> float | None:
    """Weighted logit blend over the components that exist; weights renormalize
    over present components so a missing lens never silently drags to 0.5."""
    present = [(c, w) for c, w in zip(components, weights, strict=True) if c is not None]
    if not present:
        return None
    total = sum(w for _c, w in present)
    return sum(c * w for c, w in present) / total


def nfl_model_prob(
    ml_prob: float | None,
    predicted_home_spread: float | None,
    projection_prob: float | None = None,
) -> tuple[float | None, float | None]:
    """(blended home win prob, spread-lens prob) for an NFL game.

    predicted_home_spread uses the package convention (negative = home
    favored); the margin is its negation. The projection lens (D-037) is the
    first non-market input — weights renormalize when it is absent.
    """
    spread_prob = (
        margin_to_prob(-predicted_home_spread)
        if predicted_home_spread is not None
        else None
    )
    blended = blend_logits(
        [prob_to_logit(ml_prob), prob_to_logit(spread_prob),
         prob_to_logit(projection_prob)],
        [NFL_WEIGHTS["moneyline"], NFL_WEIGHTS["spread"], NFL_WEIGHTS["projection"]],
    )
    return (logit_to_prob(blended) if blended is not None else None), spread_prob


def mlb_model_prob(
    market_prob: float | None,
    statcast_logit: float | None,
    projection_prob: float | None = None,
) -> float | None:
    """MLB blend (D-032 lenses + the D-037 projection lens), renormalized
    over present components. Without a projection this reproduces the D-032
    70/30 market/statcast split exactly (0.5/0.2 renormalized)."""
    blended = blend_logits(
        [prob_to_logit(market_prob), statcast_logit, prob_to_logit(projection_prob)],
        [MLB_WEIGHTS["market"], MLB_WEIGHTS["statcast"], MLB_WEIGHTS["projection"]],
    )
    return logit_to_prob(blended) if blended is not None else None
