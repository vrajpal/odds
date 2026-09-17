"""nflverse games data (D-046): closing spreads and totals, results and
context for every NFL game since 1999 — the history backbone of the
closing-line model. One CSV, no key, no quota:
https://github.com/nflverse/nfldata/raw/master/data/games.csv

Parsing is pure (`parse_games`) so tests run on a recorded slice; only
`fetch_games` touches the network.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date

import httpx

from mlb_odds.models import NflHistoryGame
from mlb_odds.providers.base import ProviderError
from mlb_odds.teams import NFL_CODES

logger = logging.getLogger("mlb_odds.providers.nflverse")

GAMES_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
# Franchise moves and nflverse's own abbreviations, mapped to our codes.
CODE_MAP = {"LA": "LAR", "OAK": "LV", "SD": "LAC", "STL": "LAR"}


def _int(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _float(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _team(raw: str) -> str | None:
    code = CODE_MAP.get(raw.strip(), raw.strip())
    return code if code in NFL_CODES else None


def parse_games(text: str) -> list[NflHistoryGame]:
    """Every parseable row; unknown teams are skipped with a warning."""
    out: list[NflHistoryGame] = []
    for row in csv.DictReader(io.StringIO(text)):
        home, away = _team(row["home_team"]), _team(row["away_team"])
        if home is None or away is None:
            logger.warning("skipping %s: unknown team", row.get("game_id"))
            continue
        spread = _float(row["spread_line"])
        out.append(
            NflHistoryGame(
                nflverse_id=row["game_id"],
                season=int(row["season"]),
                week=int(row["week"]),
                game_type=row["game_type"],
                gameday=date.fromisoformat(row["gameday"]),
                away_team=away,
                home_team=home,
                away_score=_int(row["away_score"]),
                home_score=_int(row["home_score"]),
                spread_line=-spread if spread is not None else None,  # to our sign convention
                total_line=_float(row["total_line"]),
                away_moneyline=_int(row["away_moneyline"]),
                home_moneyline=_int(row["home_moneyline"]),
                away_rest=_int(row["away_rest"]),
                home_rest=_int(row["home_rest"]),
                div_game=row["div_game"].strip() == "1",
                roof=row["roof"].strip() or None,
                surface=row["surface"].strip() or None,
                temp=_int(row["temp"]),
                wind=_int(row["wind"]),
                away_qb=row["away_qb_name"].strip() or None,
                home_qb=row["home_qb_name"].strip() or None,
            )
        )
    return out


class NFLverse:
    name = "nflverse"

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(transport=transport, timeout=60.0, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def fetch_games(self) -> list[NflHistoryGame]:
        try:
            response = self._client.get(GAMES_URL)
        except httpx.HTTPError as exc:
            raise ProviderError(f"nflverse unreachable: {exc}") from exc
        if response.status_code != 200:
            raise ProviderError(f"nflverse returned {response.status_code}")
        games = parse_games(response.text)
        logger.info("nflverse: %d games parsed", len(games))
        return games
