"""ESPN scoreboard provider (site.api.espn.com, unofficial/undocumented).

Free, no API key, no quota. ESPN surfaces one sportsbook's lines per event
(DraftKings at the time of recording — D-016); the book name is taken from the
response so a partner change shows up as a new book, not silent mislabeling.
Recorded fixture: tests/fixtures/espn_scoreboard_normal.json (2026-07-30).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx

from mlb_odds import teams
from mlb_odds.models import Game, GameOdds, Market, Outcome, Quote, Sport, make_game_id
from mlb_odds.providers.base import ProviderError, assign_game_numbers

logger = logging.getLogger("mlb_odds.providers.espn")

SCOREBOARD_URLS: dict[str, str] = {
    "mlb": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "nfl": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
}
SCOREBOARD_URL = SCOREBOARD_URLS["mlb"]
# ESPN's pointSpread node is the run line in baseball, the spread in football.
_SPREAD_MARKET: dict[str, Market] = {"mlb": "run_line", "nfl": "spread"}


@dataclass(frozen=True)
class FinalScore:
    """One event's score from the scoreboard, teams in canonical codes."""

    away_team: str
    home_team: str
    away_score: int
    home_score: int
    completed: bool
    start_time: datetime


class ESPN:
    name = "espn"

    def __init__(
        self,
        *,
        sport: Sport = "mlb",
        strict: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """strict=True turns data surprises (unknown teams) into errors instead of
        logged skips — used in tests. transport is injectable for fixture-based tests.
        """
        self._sport: Sport = sport
        self._scoreboard_url = SCOREBOARD_URLS[sport]
        # response market node -> (our market, sides present in that node)
        self._markets: list[tuple[str, Market, tuple[Outcome, ...]]] = [
            ("moneyline", "moneyline", ("home", "away")),
            ("pointSpread", _SPREAD_MARKET[sport], ("home", "away")),
            ("total", "total", ("over", "under")),
        ]
        self._strict = strict
        self._client = httpx.Client(transport=transport, timeout=10.0)
        self.quota_remaining: int | None = None  # unmetered — no quota to report

    def fetch_game_lines(self) -> list[GameOdds]:
        events = self._request()
        fetched_at = datetime.now(UTC)
        parsed: list[GameOdds] = []
        for event in events:
            game_odds = self._parse_event(event, fetched_at)
            if game_odds is not None:
                parsed.append(game_odds)
        return assign_game_numbers(parsed)

    def _request(self, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = self._client.get(self._scoreboard_url, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            if response.status_code >= 500:
                last_error = ProviderError(
                    f"server error {response.status_code} (attempt {attempt + 1})"
                )
                continue
            if response.status_code != 200:
                raise ProviderError(f"request failed: {response.status_code} {response.text[:200]}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise ProviderError(f"invalid JSON in response: {exc}") from exc
            events = payload.get("events") if isinstance(payload, dict) else None
            if not isinstance(events, list):
                raise ProviderError("unexpected response shape: no events list")
            return events
        raise ProviderError(f"request failed after retry: {last_error}") from last_error

    def fetch_final_scores(self, on: date) -> list[FinalScore]:
        """Scores for one scoreboard day (ESPN groups days in US/Eastern).

        Free and unmetered, like the rest of this provider. Games that are not
        completed are returned with completed=False so callers can distinguish
        "not final yet" from "not found". Unknown teams are skipped (or raised
        under strict), same policy as fetch_game_lines.
        """
        finals: list[FinalScore] = []
        for event in self._request({"dates": on.strftime("%Y%m%d")}):
            try:
                competition = event["competitions"][0]
                by_side = {c["homeAway"]: c for c in competition["competitors"]}
                raw_home = by_side["home"]["team"]["displayName"]
                raw_away = by_side["away"]["team"]["displayName"]
                home_score = int(by_side["home"].get("score") or 0)
                away_score = int(by_side["away"].get("score") or 0)
                completed = bool(event["status"]["type"]["completed"])
                start_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
            except (KeyError, IndexError, ValueError) as exc:
                logger.warning("skipping malformed scoreboard event %s: %s", event.get("id"), exc)
                continue
            try:
                home = teams.normalize(self._sport, self.name, raw_home)
                away = teams.normalize(self._sport, self.name, raw_away)
            except teams.TeamLookupError as exc:
                if self._strict:
                    raise
                logger.warning("skipping event %s: %s", event.get("id"), exc)
                continue
            finals.append(
                FinalScore(
                    away_team=away,
                    home_team=home,
                    away_score=away_score,
                    home_score=home_score,
                    completed=completed,
                    start_time=start_time,
                )
            )
        return finals

    def _parse_event(self, event: dict[str, Any], fetched_at: datetime) -> GameOdds | None:
        try:
            competition = event["competitions"][0]
            sides = {c["homeAway"]: c["team"]["displayName"] for c in competition["competitors"]}
            raw_home, raw_away = sides["home"], sides["away"]
        except (KeyError, IndexError) as exc:
            logger.warning("skipping malformed event %s: %s", event.get("id"), exc)
            return None
        try:
            home = teams.normalize(self._sport, self.name, raw_home)
            away = teams.normalize(self._sport, self.name, raw_away)
        except teams.TeamLookupError as exc:
            if self._strict:
                raise
            logger.warning("skipping event %s: %s", event.get("id"), exc)
            return None

        start_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        quotes: list[Quote] = []
        for entry in competition.get("odds", []):
            book = str(entry.get("provider", {}).get("displayName", "espn"))
            book = book.lower().replace(" ", "")
            for node_key, market, outcomes in self._markets:
                node = entry.get(node_key)
                if not isinstance(node, dict):
                    continue
                for outcome in outcomes:
                    quote = self._parse_quote(node.get(outcome), book, market, outcome)
                    if quote is not None:
                        quotes.append(quote)
        if not quotes:
            return None  # in-progress/final events carry no odds node

        # game_number is provisional here; assign_game_numbers fixes doubleheaders.
        game = Game(
            game_id=make_game_id(start_time.date().isoformat(), away, home),
            start_time=start_time,
            home_team=home,
            away_team=away,
            provider_ids={self.name: str(event["id"])},
        )
        return GameOdds(game=game, fetched_at=fetched_at, provider=self.name, quotes=quotes)

    @staticmethod
    def _parse_quote(
        side: dict[str, Any] | None, book: str, market: Market, outcome: Outcome
    ) -> Quote | None:
        """One side of one market, from its "close" (current) phase.

        ESPN's "close"/"open" naming is bookmaker jargon: "open" is where the
        line opened, "close" is where it stands now. Missing or unparsable
        odds skip the quote — SPEC FR1 stores partial results as-is.
        """
        if not isinstance(side, dict):
            return None
        phase = side.get("close")
        if not isinstance(phase, dict):
            return None
        price = _parse_american(phase.get("odds"))
        if price is None:
            return None
        line = _parse_line(phase.get("line")) if market != "moneyline" else None
        if market != "moneyline" and line is None:
            return None
        return Quote(book=book, market=market, outcome=outcome, line=line, price=price)


def _parse_american(raw: object) -> int | None:
    if not isinstance(raw, str) or not raw:
        return None
    if raw.upper() == "EVEN":
        return 100
    try:
        return int(raw)
    except ValueError:
        logger.warning("unparsable odds %r", raw)
        return None


def _parse_line(raw: object) -> float | None:
    """Spread lines look like "+1.5"/"-1.5"; total lines like "o9"/"u9.5"."""
    if not isinstance(raw, str) or not raw:
        return None
    if raw[0] in ("o", "u"):
        raw = raw[1:]
    try:
        return float(raw)
    except ValueError:
        logger.warning("unparsable line %r", raw)
        return None
