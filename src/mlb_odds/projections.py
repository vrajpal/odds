"""Third-party projection imports (D-037) — FanDuel Research CSVs first.

Exports are login-gated, so the pipeline is deliberately manual: export the
CSV in the browser, then `mlb-odds projections <file> --sport ...`. Every
import appends a timestamped snapshot; history is never overwritten, because
the history is the point — it is the accuracy ledger that will eventually
set this lens's blend weight from evidence instead of a prior.

Two shapes are understood. Game-level rows (away/home plus a win-probability
or projected-score column) map directly. The real FanDuel Research MLB export
(tests/fixtures/fanduel_research_mlb_daily.csv) turned out to be player-level
DFS projections instead — per-hitter plate appearances and runs with a
"COL @ STL" gameInfo column — so that shape is aggregated: each lineup's
projected runs are summed and normalized to a standard-length game, and the
run margin later becomes a win probability via the sport's margin sigma
(projection_prob). A file matching neither shape fails loudly, listing the
headers it found.
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
# Player-level exports (the shape FanDuel Research actually ships for MLB).
_PLAYER_TEAM_HEADERS = ("team",)
_GAMEINFO_HEADERS = ("gameinfo", "game info", "game")
_RUNS_HEADERS = ("runs",)
_PA_HEADERS = ("plateappearances", "plate appearances", "pa")

# FanDuel's abbreviations where they differ from our canonical codes.
_CODE_ALIASES: dict[str, dict[str, str]] = {
    "mlb": {
        "CHW": "CWS", "OAK": "ATH", "AZ": "ARI", "WSN": "WSH", "WAS": "WSH",
        "SDP": "SD", "SFG": "SF", "TBR": "TB", "KCR": "KC",
    },
    "nfl": {"JAC": "JAX", "WSH": "WAS", "LA": "LAR"},
}

# A full team game is ~38 plate appearances; exports list whoever is in the
# projected lineup (sometimes 8, sometimes bench extras), so raw run sums are
# biased by listing length. Normalizing to runs-per-PA x 38 removes that.
_LINEUP_PA = 38.0
_MIN_PLAYERS = 6


@dataclass(frozen=True)
class ProjectionRow:
    away_team: str
    home_team: str
    home_win_prob: float | None
    away_score: float | None
    home_score: float | None


@dataclass(frozen=True)
class PlayerProjection:
    """One player's slice of a game, canonical codes throughout — the unit
    both the CSV parser and the FanDuel Research provider aggregate from."""

    team: str
    away_team: str
    home_team: str
    runs: float
    plate_appearances: float | None


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
    code = _CODE_ALIASES[sport].get(code, code)
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
        team_h = _pick(headers, _PLAYER_TEAM_HEADERS)
        gi_h = _pick(headers, _GAMEINFO_HEADERS)
        runs_h = _pick(headers, _RUNS_HEADERS)
        if team_h and gi_h and runs_h:
            if sport != "mlb":
                raise ProjectionParseError(
                    "player-level exports are only aggregatable for mlb (runs "
                    "sum to team scores); for nfl use a game-level export"
                )
            return _parse_player_csv(
                reader, team_h, gi_h, runs_h, _pick(headers, _PA_HEADERS), sport
            )
        raise ProjectionParseError(
            "couldn't map CSV headers — need away/home team columns plus a "
            "win-probability or projected-score column (game-level), or "
            "team/gameInfo/runs columns (player-level); found: "
            f"{headers}"
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


def _parse_player_csv(
    reader: csv.DictReader[str],
    team_h: str,
    gi_h: str,
    runs_h: str,
    pa_h: str | None,
    sport: str,
) -> list[ProjectionRow]:
    """Resolve a player-level export's rows and aggregate them per game."""
    players: list[PlayerProjection] = []
    for record in reader:
        team = resolve_team(sport, record.get(team_h) or "")
        info = (record.get(gi_h) or "").split("@")
        if team is None or len(info) != 2:
            logger.warning(
                "skipping player row: unresolvable team %r or gameInfo %r",
                record.get(team_h), record.get(gi_h),
            )
            continue
        away = resolve_team(sport, info[0])
        home = resolve_team(sport, info[1])
        if away is None or home is None or team not in (away, home):
            logger.warning("skipping player row: gameInfo %r doesn't resolve "
                           "or contain team %r", record.get(gi_h), team)
            continue
        runs = _score(record.get(runs_h))
        if runs is None:
            continue
        players.append(PlayerProjection(
            team=team, away_team=away, home_team=home, runs=runs,
            plate_appearances=_score(record.get(pa_h)) if pa_h else None,
        ))
    return aggregate_players(players)


def aggregate_players(players: list[PlayerProjection]) -> list[ProjectionRow]:
    """Aggregate per-player projections into one row per game.

    Team score = sum of the lineup's projected runs, normalized to a
    _LINEUP_PA-length game so an 8-hitter listing isn't undercounted relative
    to a 10-hitter one. The margin is neutral-site (no home-field bump) —
    a documented bias the accuracy ledger will quantify.
    """
    sums: dict[tuple[str, str], dict[str, list[float]]] = {}
    for p in players:
        entry = sums.setdefault((p.away_team, p.home_team), {}).setdefault(
            p.team, [0.0, 0.0, 0.0]
        )
        entry[0] += p.runs
        entry[1] += p.plate_appearances or 0.0
        entry[2] += 1
    rows: list[ProjectionRow] = []
    for (away, home), by_team in sorted(sums.items()):
        if any(
            code not in by_team or by_team[code][2] < _MIN_PLAYERS
            for code in (away, home)
        ):
            logger.warning("skipping game %s @ %s: fewer than %d projected "
                           "players on a side", away, home, _MIN_PLAYERS)
            continue

        scores = {}
        for code in (away, home):
            runs, pa, _n = by_team[code]
            scores[code] = round(runs * (_LINEUP_PA / pa) if pa else runs, 2)
        rows.append(
            ProjectionRow(
                away_team=away, home_team=home, home_win_prob=None,
                away_score=scores[away], home_score=scores[home],
            )
        )
    if not rows:
        raise ProjectionParseError(
            "no aggregatable games in the player-level projections"
        )
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
