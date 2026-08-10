"""FanDuel Research (numberFire) MLB projections — free, no auth (D-037).

The site is a Next.js app. Daily DFS slate ids are embedded in the batters
page's __NEXT_DATA__ blob, and the projections themselves come from an open
GraphQL endpoint that accepts plain query documents (verified live
2026-08-10; introspection is enabled, so field drift is diagnosable). We
query batters only: their projected runs are already opponent-and-park
adjusted, so lineup run sums are a complete team-scoring estimate — folding
in pitcher projections would double-count opponent pitching.

DAILY projections require a slateId. Slates are DFS contest groupings
("Main", "All Day", per-game); "All Day" covers the fullest schedule, with
"Main" as fallback.

Recorded fixtures: tests/fixtures/fanduel_research_page.html (trimmed to the
__NEXT_DATA__ script, labeled inside) and
tests/fixtures/fanduel_research_graphql_batters.json (trimmed rows, labeled
inside).
"""

import json
import logging
import re
from typing import Any

import httpx

from mlb_odds.projections import PlayerProjection, resolve_team
from mlb_odds.providers.base import ProviderError

logger = logging.getLogger("mlb_odds.providers.fanduel_research")

PAGE_URL = "https://www.fanduel.com/research/mlb/fantasy/dfs-projections/batters"
GRAPHQL_URL = "https://www.fanduel.com/research/api/graphql"
_UA = "Mozilla/5.0 (X11; Linux x86_64) mlb-odds/1.0"
_PREFERRED_SLATES = ("All Day", "Main")

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)

_QUERY = """
query Projections($input: ProjectionsInput!) {
  getProjections(input: $input) {
    ... on MlbBatter {
      player { name }
      team { abbreviation }
      plateAppearances
      runs
      gameInfo {
        awayTeam { abbreviation }
        homeTeam { abbreviation }
        gameTime
      }
    }
  }
}
"""


class FanDuelResearch:
    """Fetches today's MLB batter projections as PlayerProjection rows."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            timeout=30.0,
            headers={"User-Agent": _UA, "Cache-Control": "no-cache"},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def _slate_id(self) -> str:
        try:
            resp = self._client.get(PAGE_URL)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"fanduel_research page fetch failed: {exc}") from exc
        match = _NEXT_DATA_RE.search(resp.text)
        if match is None:
            raise ProviderError(
                "fanduel_research page has no __NEXT_DATA__ blob — layout changed"
            )
        try:
            data = json.loads(match.group(1))
            slates = data["props"]["pageProps"]["projectionInfo"]["slatesFilter"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ProviderError(
                f"fanduel_research page data shape changed: {exc}"
            ) from exc
        by_label = {s.get("label"): s.get("value") for s in slates if s.get("value")}
        for label in _PREFERRED_SLATES:
            if label in by_label:
                logger.info("using slate %r (%s)", label, by_label[label])
                return str(by_label[label])
        raise ProviderError(
            f"no usable slate today (found: {sorted(by_label)})"
        )

    def fetch_mlb_batters(self) -> list[PlayerProjection]:
        """Today's batter projections, aggregation-ready. Rows with teams we
        can't resolve are skipped with a warning, not fatal."""
        slate_id = self._slate_id()
        body = {
            "query": _QUERY,
            "variables": {
                "input": {
                    "type": "DAILY", "sport": "MLB",
                    "position": "MLB_BATTER", "slateId": slate_id,
                }
            },
        }
        try:
            resp = self._client.post(GRAPHQL_URL, json=body)
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"fanduel_research graphql failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"fanduel_research graphql returned non-JSON: {exc}"
            ) from exc
        if payload.get("errors"):
            raise ProviderError(
                f"fanduel_research graphql errors: {payload['errors']}"
            )
        raw: list[dict[str, Any]] = payload.get("data", {}).get("getProjections") or []
        players: list[PlayerProjection] = []
        for row in raw:
            info = row.get("gameInfo") or {}
            team = resolve_team("mlb", (row.get("team") or {}).get("abbreviation") or "")
            away = resolve_team(
                "mlb", (info.get("awayTeam") or {}).get("abbreviation") or ""
            )
            home = resolve_team(
                "mlb", (info.get("homeTeam") or {}).get("abbreviation") or ""
            )
            runs = row.get("runs")
            if (
                team is None or away is None or home is None
                or team not in (away, home) or not isinstance(runs, int | float)
            ):
                logger.warning(
                    "skipping row: %s (%s) in %s @ %s",
                    (row.get("player") or {}).get("name"), row.get("team"),
                    info.get("awayTeam"), info.get("homeTeam"),
                )
                continue
            pa = row.get("plateAppearances")
            players.append(
                PlayerProjection(
                    team=team, away_team=away, home_team=home, runs=float(runs),
                    plate_appearances=float(pa) if isinstance(pa, int | float) else None,
                )
            )
        if not players:
            raise ProviderError("fanduel_research returned no usable batter rows")
        return players
