"""One game, every priced side, ranked by value, with the context that
explains the ranking (D-045).

The dashboard answers "which games have value today"; this answers "for
THIS game, which of the dozens of quotes across books and markets is worth
taking, and why". Every side is priced two ways:

- fair: what the market as a whole implies for that side AT THAT BOOK'S
  LINE. Moneylines use the devigged consensus. Spreads and totals go
  through the sport's normal margin model centred on the market's expected
  margin (NFL: the consensus spread; MLB: the moneyline consensus, since the
  run line is the derivative there) — so a book hanging a different number
  is priced for the number it hangs, never compared across a 3-run gap as if
  it were the same bet.
- model: the same conversion centred on the model's expected margin, when
  the sport's blend has a view (moneylines and spreads; totals have none).

EV is per unit staked. `line_edge` is the points the book gives that side
beyond the consensus number (positive = better than consensus). The normal
approximations are literature anchors, not fits — the margin sigmas are the
package's (D-036), the total sigmas below are the usual empirical ones.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import NormalDist, median

from mlb_odds import contest, model, valuation
from mlb_odds.models import Game, Quote

MARGIN_SIGMA = {"nfl": model.NFL_MARGIN_SIGMA, "mlb": model.MLB_MARGIN_SIGMA}
TOTAL_SIGMA = {"nfl": 13.6, "mlb": 4.2}  # empirical sd of total points / runs
SPREAD_MARKET = {"nfl": "spread", "mlb": "run_line"}
_MARKET_ORDER = {"moneyline": 0, "spread": 1, "run_line": 1, "total": 2}
_NORMAL = NormalDist()
# A book whose newest quote on a market is this much older than the game's
# newest snapshot has stopped reporting: its carried-forward number is not
# an offer anyone can take, so it is shown but never ranked or starred.
STALE_AFTER = timedelta(hours=24)


def cover_prob(expected_home_margin: float, home_line: float, sigma: float) -> float:
    """P(home covers a home line) under margin ~ N(expected, sigma):
    P(margin + line > 0). Pushes are folded into the loss side."""
    return round(_NORMAL.cdf((expected_home_margin + home_line) / sigma), 4)


def over_prob(expected_total: float, line: float, sigma: float) -> float:
    return round(1.0 - _NORMAL.cdf((line - expected_total) / sigma), 4)


def margin_from_prob(home_prob: float, sigma: float) -> float:
    """Expected home margin implied by a straight-up probability."""
    p = min(max(home_prob, 0.001), 0.999)
    return round(_NORMAL.inv_cdf(p) * sigma, 2)


@dataclass(frozen=True)
class MarketRow:
    market: str
    side: str  # home | away | over | under
    label: str  # "KC -3", "over 44.5", "P. Mahomes over 275.5"
    book: str
    price: int
    line: float | None
    team: str | None = None
    player: str | None = None
    fair_prob: float | None = None
    ev: float | None = None
    model_prob: float | None = None
    model_ev: float | None = None
    line_edge: float | None = None  # points beyond the consensus line, + = better
    key_numbers: list[float] = field(default_factory=list)
    best: bool = False  # best EV for this (market, side, player), among fresh quotes
    quoted_at: datetime | None = None  # newest snapshot of this book on this market
    stale: bool = False  # quoted_at lags the game's newest snapshot by > STALE_AFTER


@dataclass(frozen=True)
class Consensus:
    moneyline_home: float | None  # devigged consensus, home
    spread: float | None  # median home line
    total: float | None  # median total
    expected_margin: float | None  # market's expected home margin (points/runs)
    model_margin: float | None  # model's expected home margin


def _ev(prob: float | None, price: int) -> float | None:
    return valuation.expected_value(prob, price) if prob is not None else None


def _pair_by_book(quotes: Sequence[Quote], market: str) -> dict[str, dict[str, Quote]]:
    out: dict[str, dict[str, Quote]] = {}
    for q in quotes:
        if q.market == market and q.player is None:
            out.setdefault(q.book, {})[q.outcome] = q
    return out


def build_rows(
    sport: str,
    game: Game,
    quotes: Sequence[Quote],
    *,
    fair_home: float | None,
    model_home: float | None,
    predicted_margin: float | None,
    quoted_at: Mapping[tuple[str, str], datetime] | None = None,
) -> tuple[list[MarketRow], Consensus]:
    """Every priced side of the game, ranked by EV against the market fair.

    `quoted_at` maps (book, market) to that book's newest snapshot on that
    market; rows older than the game's newest by STALE_AFTER are flagged
    stale, sorted after fresh rows, and never marked best."""
    sigma = MARGIN_SIGMA[sport]
    spread_market = SPREAD_MARKET[sport]
    spreads = _pair_by_book(quotes, spread_market)
    totals = _pair_by_book(quotes, "total")
    moneylines = _pair_by_book(quotes, "moneyline")

    home_lines = [
        p["home"].line for p in spreads.values() if "home" in p and p["home"].line is not None
    ]
    total_lines: list[float] = []
    for pair in totals.values():
        first = next(iter(pair.values()), None)
        if first is not None and first.line is not None:
            total_lines.append(first.line)
    consensus_spread = float(median(home_lines)) if home_lines else None
    consensus_total = float(median(total_lines)) if total_lines else None

    # The market's expected home margin: NFL trusts the spread market, MLB the
    # moneyline (its run line is a fixed ±1.5 derivative).
    if sport == "nfl" and consensus_spread is not None:
        expected = -consensus_spread
    elif fair_home is not None:
        expected = margin_from_prob(fair_home, sigma)
    else:
        expected = None
    if predicted_margin is not None:
        model_margin: float | None = predicted_margin
    elif model_home is not None:
        model_margin = margin_from_prob(model_home, sigma)
    else:
        model_margin = None

    rows: list[MarketRow] = []
    for book, pair in sorted(moneylines.items()):
        for side in ("home", "away"):
            q = pair.get(side)
            if q is None:
                continue
            fair = (
                fair_home if side == "home" else (1 - fair_home if fair_home is not None else None)
            )
            mp = (
                model_home
                if side == "home"
                else (1 - model_home if model_home is not None else None)
            )
            team = game.home_team if side == "home" else game.away_team
            rows.append(
                MarketRow(
                    market="moneyline",
                    side=side,
                    label=f"{team} ML",
                    book=book,
                    price=q.price,
                    line=None,
                    team=team,
                    fair_prob=_r(fair),
                    ev=_ev(fair, q.price),
                    model_prob=_r(mp),
                    model_ev=_ev(mp, q.price),
                )
            )

    for book, pair in sorted(spreads.items()):
        home_q = pair.get("home")
        home_line = (
            home_q.line
            if home_q is not None
            else (-pair["away"].line if "away" in pair and pair["away"].line is not None else None)
        )
        if home_line is None:
            continue
        p_home = cover_prob(expected, home_line, sigma) if expected is not None else None
        m_home = cover_prob(model_margin, home_line, sigma) if model_margin is not None else None
        keys = (
            contest.key_numbers_crossed(home_line, consensus_spread)
            if sport == "nfl" and consensus_spread is not None
            else []
        )
        for side in ("home", "away"):
            q = pair.get(side)
            if q is None:
                continue
            fair = p_home if side == "home" else (1 - p_home if p_home is not None else None)
            mp = m_home if side == "home" else (1 - m_home if m_home is not None else None)
            team = game.home_team if side == "home" else game.away_team
            side_line = home_line if side == "home" else -home_line
            edge = None
            if consensus_spread is not None:
                edge = round(
                    side_line - (consensus_spread if side == "home" else -consensus_spread), 2
                )
            rows.append(
                MarketRow(
                    market=spread_market,
                    side=side,
                    label=f"{team} {side_line:+g}",
                    book=book,
                    price=q.price,
                    line=side_line,
                    team=team,
                    fair_prob=_r(fair),
                    ev=_ev(fair, q.price),
                    model_prob=_r(mp),
                    model_ev=_ev(mp, q.price),
                    line_edge=edge,
                    key_numbers=list(keys),
                )
            )

    tsig = TOTAL_SIGMA[sport]
    for book, pair in sorted(totals.items()):
        for side in ("over", "under"):
            q = pair.get(side)
            if q is None or q.line is None:
                continue
            p_over = (
                over_prob(consensus_total, q.line, tsig) if consensus_total is not None else None
            )
            fair = p_over if side == "over" else (1 - p_over if p_over is not None else None)
            edge = None
            if consensus_total is not None:
                edge = round(
                    (consensus_total - q.line) if side == "over" else (q.line - consensus_total), 2
                )
            rows.append(
                MarketRow(
                    market="total",
                    side=side,
                    label=f"{side} {q.line:g}",
                    book=book,
                    price=q.price,
                    line=q.line,
                    fair_prob=_r(fair),
                    ev=_ev(fair, q.price),
                    line_edge=edge,
                )
            )

    rows.extend(_prop_rows(quotes))
    rows = _stamp_freshness(rows, quoted_at or {})
    rows.sort(
        key=lambda r: (r.stale, r.ev is None, -(r.ev or 0), _MARKET_ORDER.get(r.market, 9), r.book)
    )
    best_seen: set[tuple[str, str, str | None, float | None]] = set()
    marked: list[MarketRow] = []
    for r in rows:  # fresh rows come first, EV-sorted: the first of each side is its best
        key = (r.market, r.side, r.player, r.line if r.player else None)
        if r.ev is not None and not r.stale and key not in best_seen:
            best_seen.add(key)
            r = MarketRow(**{**r.__dict__, "best": True})
        marked.append(r)
    return marked, Consensus(
        moneyline_home=_r(fair_home),
        spread=consensus_spread,
        total=consensus_total,
        expected_margin=_r(expected, 2),
        model_margin=_r(model_margin, 2),
    )


def _stamp_freshness(
    rows: list[MarketRow], quoted_at: Mapping[tuple[str, str], datetime]
) -> list[MarketRow]:
    if not quoted_at:
        return rows
    newest = max(quoted_at.values())
    out = []
    for r in rows:
        at = quoted_at.get((r.book, r.market))
        stale = at is not None and newest - at > STALE_AFTER
        out.append(MarketRow(**{**r.__dict__, "quoted_at": at, "stale": stale}))
    return out


def _prop_rows(quotes: Sequence[Quote]) -> list[MarketRow]:
    """Player props: each book's over/under pair devigged; the fair for a
    (player, market, line) is the median across books quoting that exact
    line, so a lone book is judged against itself (EV ~ -vig)."""
    pairs: dict[tuple[str, str, float, str], dict[str, Quote]] = {}
    for q in quotes:
        if q.player is None or q.line is None or q.outcome not in ("over", "under"):
            continue
        pairs.setdefault((q.market, q.player, q.line, q.book), {})[q.outcome] = q
    devigged: dict[tuple[str, str, float], dict[str, float]] = {}
    for (market, player, line, book), pair in pairs.items():
        if "over" in pair and "under" in pair:
            devigged.setdefault((market, player, line), {})[book] = valuation.devig_pair(
                pair["over"].price, pair["under"].price
            )
    rows = []
    for (market, player, line, book), pair in sorted(pairs.items()):
        probs = devigged.get((market, player, line), {})
        fair_over = float(median(probs.values())) if probs else None
        for side in ("over", "under"):
            quote = pair.get(side)
            if quote is None:
                continue
            fair = (
                fair_over if side == "over" else (1 - fair_over if fair_over is not None else None)
            )
            rows.append(
                MarketRow(
                    market=market,
                    side=side,
                    label=f"{player} {side} {line:g}",
                    book=book,
                    price=quote.price,
                    line=line,
                    player=player,
                    fair_prob=_r(fair),
                    ev=_ev(fair, quote.price),
                )
            )
    return rows


def _r(v: float | None, places: int = 4) -> float | None:
    return round(v, places) if v is not None else None
