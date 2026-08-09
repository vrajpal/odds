"""Statcast scouting data (D-031): probable starters and expected stats.

Two free sources feed the per-game matchup card:
- **MLB Stats API** (statsapi.mlb.com): the day's schedule with probable
  starters and doubleheader numbering — the single biggest odds-mover in
  baseball is who starts.
- **Baseball Savant** expected-stats leaderboards (CSV): team batting and
  pitcher xBA/xSLG/xwOBA (+xERA), which strip batted-ball luck from results —
  the gap between expected and actual is exactly what markets price slowly.

Both are fetched whole and upserted; no API keys, no metering.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from mlb_odds import teams
from mlb_odds.providers.base import ProviderError

logger = logging.getLogger("mlb_odds.statcast")

SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
SAVANT_URL = "https://baseballsavant.mlb.com/leaderboard/expected_statistics"

# Savant team_id abbreviations that differ from our canonical codes.
_SAVANT_TEAM_FIX = {"AZ": "ARI"}


@dataclass(frozen=True)
class ProbableGame:
    away_team: str
    home_team: str
    start_time: datetime
    game_number: int
    away_pitcher: str | None
    home_pitcher: str | None


@dataclass(frozen=True)
class TeamExpected:
    team: str
    season: int
    pa: int
    xba: float | None
    xslg: float | None
    xwoba: float | None


@dataclass(frozen=True)
class PitcherExpected:
    name: str  # "First Last"
    season: int
    pa: int
    xba: float | None
    xslg: float | None
    xwoba: float | None
    xera: float | None


class StatcastSource:
    """Fetches statsapi + Savant; transport injectable for fixture tests."""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(transport=transport, timeout=30.0)

    def probables(self, on: date) -> list[ProbableGame]:
        """The day's schedule with probable starters, teams canonicalized."""
        payload = self._get_json(
            SCHEDULE_URL,
            {"sportId": "1", "date": on.isoformat(), "hydrate": "probablePitcher"},
        )
        dates = payload.get("dates")
        first = dates[0] if isinstance(dates, list) and dates else {}
        games_raw = first.get("games", []) if isinstance(first, dict) else []
        out: list[ProbableGame] = []
        for g in games_raw:
            try:
                away_raw = g["teams"]["away"]["team"]["name"]
                home_raw = g["teams"]["home"]["team"]["name"]
                start = datetime.fromisoformat(g["gameDate"].replace("Z", "+00:00"))
                number = int(g.get("gameNumber") or 1)
            except (KeyError, ValueError) as exc:
                logger.warning("skipping malformed schedule entry: %s", exc)
                continue
            try:
                away = teams.normalize("mlb", "statsapi", away_raw)
                home = teams.normalize("mlb", "statsapi", home_raw)
            except teams.TeamLookupError as exc:
                logger.warning("skipping game: %s", exc)
                continue
            out.append(
                ProbableGame(
                    away_team=away,
                    home_team=home,
                    start_time=start,
                    game_number=number,
                    away_pitcher=(g["teams"]["away"].get("probablePitcher") or {}).get(
                        "fullName"
                    ),
                    home_pitcher=(g["teams"]["home"].get("probablePitcher") or {}).get(
                        "fullName"
                    ),
                )
            )
        return out

    def team_expected(self, season: int) -> list[TeamExpected]:
        rows = self._get_csv(
            {"type": "batter-team", "year": str(season), "position": "", "team": "",
             "filterType": "bip", "min": "q", "csv": "true"}
        )
        out = []
        for row in rows:
            abbrev = row.get("team_id", "")
            team = _SAVANT_TEAM_FIX.get(abbrev, abbrev)
            if team not in teams.MLB_CODES:
                logger.warning("unknown savant team abbrev %r", abbrev)
                continue
            out.append(
                TeamExpected(
                    team=team,
                    season=season,
                    pa=int(row.get("pa") or 0),
                    xba=_num(row.get("est_ba")),
                    xslg=_num(row.get("est_slg")),
                    xwoba=_num(row.get("est_woba")),
                )
            )
        return out

    def pitcher_expected(self, season: int, *, min_pa: int = 50) -> list[PitcherExpected]:
        rows = self._get_csv(
            {"type": "pitcher", "year": str(season), "position": "", "team": "",
             "filterType": "bip", "min": str(min_pa), "csv": "true"}
        )
        out = []
        for row in rows:
            raw_name = row.get("last_name, first_name") or row.get("player_name") or ""
            if "," in raw_name:
                last, _, first = raw_name.partition(",")
                name = f"{first.strip()} {last.strip()}"
            else:
                name = raw_name.strip()
            if not name:
                continue
            out.append(
                PitcherExpected(
                    name=name,
                    season=season,
                    pa=int(row.get("pa") or 0),
                    xba=_num(row.get("est_ba")),
                    xslg=_num(row.get("est_slg")),
                    xwoba=_num(row.get("est_woba")),
                    xera=_num(row.get("xera")),
                )
            )
        return out

    def _get_json(self, url: str, params: dict[str, str]) -> dict[str, object]:
        resp = self._request(url, params)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderError(f"invalid JSON from {url}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ProviderError(f"unexpected response shape from {url}")
        return payload

    def _get_csv(self, params: dict[str, str]) -> list[dict[str, str]]:
        resp = self._request(SAVANT_URL, params)
        text = resp.text.lstrip("﻿")
        return list(csv.DictReader(io.StringIO(text)))

    def _request(self, url: str, params: dict[str, str]) -> httpx.Response:
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                resp = self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            if resp.status_code >= 500:
                last_error = ProviderError(f"server error {resp.status_code}")
                continue
            if resp.status_code != 200:
                raise ProviderError(f"request failed: {resp.status_code} {url}")
            return resp
        raise ProviderError(f"request failed after retry: {last_error}") from last_error


def _num(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None
