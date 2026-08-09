"""Third-party projection imports (D-037) — FanDuel Research CSVs first.

Exports are login-gated, so the pipeline is deliberately manual: export the
CSV in the browser, then `mlb-odds projections <file> --sport ...`. Every
import appends a timestamped snapshot; history is never overwritten, because
the history is the point — it is the accuracy ledger that will eventually
set this lens's blend weight from evidence instead of a prior.

The parser is header-tolerant (synonym lists below) and locked down further
against real exports as they arrive. A file whose headers can't be mapped
fails loudly, listing what it found.
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from mlb_odds import teams

logger = logging.getLogger("mlb_odds.projections")

DEFAULT_SOURCE = "fanduel_research"

# Header synonyms, lowercased/stripped. Extend as real exports arrive.
_AWAY_HEADERS = ("away", "away team", "away_team", "visitor", "road team")
_HOME_HEADERS = ("home", "home team", "home_team")
_HOME_PROB_HEADERS = (
    "home win%", "home win %", "home win prob", "home_win_prob",
    "home win probability", "win% (home)", "home%",
)
_AWAY_PROB_HEADERS = (
    "away win%", "away win %", "away win prob", "away_win_prob",
    "away win probability", "win% (away)", "away%",
)
_HOME_SCORE_HEADERS = (
    "home score", "home_score", "proj home score", "projected home score",
    "home pts", "home points", "home runs",
)
_AWAY_SCORE_HEADERS = (
    "away score", "away_score", "proj away score", "projected away score",
    "away pts", "away points", "away runs",
)


@dataclass(frozen=True)
class ProjectionRow:
    away_team: str
    home_team: str
    home_win_prob: float | None
    away_score: float | None
    home_score: float | None


class ProjectionParseError(ValueError):
    """The CSV's headers couldn't be mapped; message lists what was found."""


def resolve_team(sport: str, raw: str) -> str | None:
    """Best-effort team resolution: full name via the registry, canonical
    code as-is, or unique nickname suffix ("Yankees" -> NYY)."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return teams.normalize(sport, "the_odds_api", raw)
    except teams.TeamLookupError:
        pass
    code = raw.upper()
    if code in teams.CANONICAL_CODES[sport]:
        return code
    mapping = teams._PROVIDER_MAPPINGS[(sport, "the_odds_api")]
    suffix_hits = {
        canonical
        for full, canonical in mapping.items()
        if full.lower().endswith(raw.lower())
    }
    if len(suffix_hits) == 1:
        return suffix_hits.pop()
    return None


def _pick(headers: Sequence[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {h.lower().strip(): h for h in headers}
    for cand in candidates:
        if cand in lowered:
            return lowered[cand]
    return None


def _prob(raw: str | None) -> float | None:
    if raw is None:
        return None
    raw = raw.strip().rstrip("%")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if value > 1.0:  # percentage form
        value /= 100.0
    return round(value, 4) if 0.0 <= value <= 1.0 else None


def _score(raw: str | None) -> float | None:
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_csv(text: str, sport: str) -> list[ProjectionRow]:
    """Parse an exported projections CSV into normalized rows.

    Requires away+home team columns plus at least one of: a win-probability
    pair/column or projected scores. Unresolvable team names are skipped
    with a warning (an all-star row or a header repeat shouldn't kill the
    import); zero resolvable rows is an error.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    headers: Sequence[str] = reader.fieldnames or []
    away_h = _pick(headers, _AWAY_HEADERS)
    home_h = _pick(headers, _HOME_HEADERS)
    hp_h = _pick(headers, _HOME_PROB_HEADERS)
    ap_h = _pick(headers, _AWAY_PROB_HEADERS)
    hs_h = _pick(headers, _HOME_SCORE_HEADERS)
    as_h = _pick(headers, _AWAY_SCORE_HEADERS)
    if away_h is None or home_h is None or (hp_h is None and ap_h is None and hs_h is None):
        raise ProjectionParseError(
            "couldn't map CSV headers — need away/home team columns and a win-"
            f"probability or projected-score column; found: {headers}"
        )
    rows: list[ProjectionRow] = []
    for record in reader:
        away = resolve_team(sport, record.get(away_h) or "")
        home = resolve_team(sport, record.get(home_h) or "")
        if away is None or home is None:
            logger.warning(
                "skipping row: unresolvable team(s) %r / %r",
                record.get(away_h), record.get(home_h),
            )
            continue
        home_prob = _prob(record.get(hp_h)) if hp_h else None
        if home_prob is None and ap_h:
            away_prob = _prob(record.get(ap_h))
            home_prob = round(1.0 - away_prob, 4) if away_prob is not None else None
        rows.append(
            ProjectionRow(
                away_team=away,
                home_team=home,
                home_win_prob=home_prob,
                away_score=_score(record.get(as_h)) if as_h else None,
                home_score=_score(record.get(hs_h)) if hs_h else None,
            )
        )
    if not rows:
        raise ProjectionParseError("no rows with resolvable teams in the CSV")
    return rows


def projection_prob(
    home_win_prob: float | None,
    away_score: float | None,
    home_score: float | None,
    sport: str,
) -> float | None:
    """The lens probability from a snapshot: the stated win probability when
    present, else derived from projected scores via the sport's margin
    distribution."""
    if home_win_prob is not None:
        return home_win_prob
    if away_score is None or home_score is None:
        return None
    from mlb_odds.model import MLB_MARGIN_SIGMA, NFL_MARGIN_SIGMA, margin_to_prob

    sigma = NFL_MARGIN_SIGMA if sport == "nfl" else MLB_MARGIN_SIGMA
    return margin_to_prob(home_score - away_score, sigma)


def brier(outcomes: list[tuple[float, int]]) -> float | None:
    """Mean Brier score over (predicted home prob, home_won 0/1). Lower is
    better; 0.25 = coin-flip forecaster."""
    if not outcomes:
        return None
    return round(
        sum((p - won) ** 2 for p, won in outcomes) / len(outcomes), 4
    )
