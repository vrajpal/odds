"""FastAPI server for MLB/NFL odds — REST API and static frontend.

Every data endpoint takes `?sport=mlb|nfl` (default mlb) and reads that
sport's own database (D-019: one file per sport)."""

import logging
import os
import sqlite3
from datetime import datetime, time, timedelta, tzinfo
from pathlib import Path
from time import monotonic
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mlb_odds.client import OddsClient
from mlb_odds.models import Quote, Sport
from mlb_odds.providers.espn import ESPN

logger = logging.getLogger(__name__)

app = FastAPI(title="Odds API", description="REST API for MLB/NFL betting odds")

_DB_ENV = {"mlb": "MLB_ODDS_DB", "nfl": "NFL_ODDS_DB"}
_DB_DEFAULT = {"mlb": "./odds.sqlite", "nfl": "./nfl-odds.sqlite"}
# The board's middle column is one semantic market with sport-local names.
_SPREAD_MARKET = {"mlb": "run_line", "nfl": "spread"}


def _resolve_db(sport: Sport) -> Path:
    """The database path is deployment configuration, never request input.

    An earlier revision exposed this as a `db` query parameter on every
    endpoint. Because Storage opens read-write and migrates, that let any
    unauthenticated GET create a SQLite file at an arbitrary path, or add this
    schema to an unrelated SQLite database on the host. Server-side only —
    the request only picks which sport's configured database to read (D-019).
    """
    env = os.environ.get(_DB_ENV[sport])
    return Path(env) if env else Path(_DB_DEFAULT[sport])


def _local_tz() -> tzinfo:
    tz = datetime.now().astimezone().tzinfo
    assert tz is not None
    return tz


def _local_day_window(tz: tzinfo) -> tuple[datetime, datetime]:
    """Half-open UTC [start, end) instants bounding the current local day.

    The board is a local-day view, but start_time is stored UTC, so a UTC
    calendar-date filter is the wrong question: a 10pm PDT first pitch is the
    next day in UTC and would drop out of the board entirely.
    """
    today = datetime.now(tz).date()
    start = datetime.combine(today, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def _get_client(sport: Sport) -> OddsClient:
    """Read-only, provider-less client for one sport's database.

    `providers=[]` is what keeps HTTP traffic from reaching The Odds API and
    burning metered credits; OddsClient enforces that read_only and providers
    are mutually exclusive so this can't regress silently.
    """
    try:
        return OddsClient(providers=[], db=_resolve_db(sport), read_only=True)
    except sqlite3.OperationalError as exc:
        flag = "" if sport == "mlb" else f" --sport {sport}"
        raise HTTPException(
            status_code=503,
            detail=f"Odds database unavailable. Run `mlb-odds collect --once{flag}` to create it.",
        ) from exc


class Game(BaseModel):
    game_id: str
    away_team: str
    home_team: str
    start_time: str


class QuoteResponse(BaseModel):
    book: str
    market: str
    outcome: str
    price: int
    line: float | None = None


class GameOddsData(BaseModel):
    game: Game
    fetched_at: str
    quotes: list[QuoteResponse]


class GameBoard(BaseModel):
    game: Game
    books: dict[str, dict[str, str]]  # { "book": { "moneyline": "-150/+130", ... } }


@app.get("/api/today", response_model=list[GameBoard])
def get_today(sport: Literal["mlb", "nfl"] = "mlb") -> list[GameBoard]:
    """Get today's games with latest odds per book, from the sport's database."""
    client = _get_client(sport)
    try:
        tz = _local_tz()
        # Narrow in SQL. Fetching every snapshot ever stored and filtering in
        # Python made this cost grow with the whole append-only odds table
        # while still returning ~15 games.
        board = client.current_odds(window=_local_day_window(tz))
        if not board:
            return []

        result: dict[str, GameBoard] = {}
        for go in board:
            game_id = go.game.game_id
            if game_id not in result:
                result[game_id] = GameBoard(
                    game=Game(
                        game_id=go.game.game_id,
                        away_team=go.game.away_team,
                        home_team=go.game.home_team,
                        start_time=go.game.start_time.astimezone(tz).isoformat(),
                    ),
                    books={},
                )

            for book in {q.book for q in go.quotes}:
                quotes = [q for q in go.quotes if q.book == book]
                spread_market = _SPREAD_MARKET[sport]
                result[game_id].books[book] = {
                    "moneyline": _fmt_moneyline(quotes),
                    spread_market: _fmt_spread(quotes, spread_market),
                    "total": _fmt_total(quotes),
                }

        return sorted(result.values(), key=lambda gb: gb.game.start_time)
    finally:
        client.close()


@app.get("/api/games/{game_id}/history")
def get_game_history(game_id: str, sport: Literal["mlb", "nfl"] = "mlb") -> dict[str, object]:
    """Get line movement history for a game from the sport's database."""
    client = _get_client(sport)
    try:
        df = client.history_df(game_id)
        if df.empty:
            raise HTTPException(status_code=404, detail=f"No odds found for {game_id}")

        df["fetched_at"] = df["fetched_at"].dt.tz_convert(_local_tz())
        return {
            "game_id": game_id,
            "rows": df.to_dict(orient="records"),
            "count": len(df),
        }
    finally:
        client.close()


@app.get("/api/export")
def export_odds(fmt: str = "csv", sport: Literal["mlb", "nfl"] = "mlb") -> dict[str, object]:
    """Export all stored odds for one sport to CSV or JSON."""
    if fmt not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'json'")

    client = _get_client(sport)
    try:
        df = client.odds_df()
        if df.empty:
            raise HTTPException(status_code=404, detail="No odds stored")

        if fmt == "json":
            return {"format": "json", "data": df.to_dict(orient="records"), "count": len(df)}

        csv_data = df.to_csv(index=False)
        return {"format": "csv", "data": csv_data, "count": len(df)}
    finally:
        client.close()


def _find(quotes: list[Quote], market: str, outcome: str) -> Quote | None:
    return next(
        (q for q in quotes if q.market == market and q.outcome == outcome),
        None,
    )


def _fmt_moneyline(quotes: list[Quote]) -> str:
    away = _find(quotes, "moneyline", "away")
    home = _find(quotes, "moneyline", "home")
    if away is None or home is None:
        return "-"
    return f"{away.price:+d}/{home.price:+d}"


def _fmt_spread(quotes: list[Quote], market: str) -> str:
    home = _find(quotes, market, "home")
    if home is None or home.line is None:
        return "-"
    return f"{home.line:+.1f} ({home.price:+d})"


def _fmt_total(quotes: list[Quote]) -> str:
    over = _find(quotes, "total", "over")
    if over is None or over.line is None:
        return "-"
    return f"{over.line:.1f} (o{over.price:+d})"


# Manual refresh (D-029): the one deliberate exception to "the API never
# reaches a provider" (D-012). Constrained three ways: ESPN only (free,
# unmetered — constructing TheOddsAPI here would let HTTP spend credits),
# debounced per sport, and served only on this app, which is not exposed
# through the public tunnel.
_REFRESH_MIN_INTERVAL = 300.0  # seconds
_last_refresh: dict[str, float] = {}


@app.post("/api/refresh")
def refresh(sport: Literal["mlb", "nfl"] = "mlb") -> dict[str, object]:
    """Pull current lines from the free ESPN provider into the sport's DB."""
    now = monotonic()
    last = _last_refresh.get(sport)
    if last is not None and (elapsed := now - last) < _REFRESH_MIN_INTERVAL:
        raise HTTPException(
            status_code=429,
            detail=f"refreshed {int(elapsed)}s ago; retry in "
            f"{int(_REFRESH_MIN_INTERVAL - elapsed)}s",
        )
    client = OddsClient(providers=[ESPN(sport=sport)], db=_resolve_db(sport))
    try:
        results = client.fetch_and_store()
        errors = {name: str(exc) for name, exc in client.last_errors.items()}
    finally:
        client.close()
    _last_refresh[sport] = now
    rows = sum(len(go.quotes) for go in results)
    logger.info("manual refresh (%s): %d games, %d rows", sport, len(results), rows)
    return {"sport": sport, "games": len(results), "rows": rows, "errors": errors}


@app.get("/api/health")
def health() -> dict[str, str]:
    """API health check."""
    return {"status": "ok", "version": "0.1.0"}


def _frontend_dist() -> Path:
    env = os.environ.get("MLB_ODDS_FRONTEND_DIST")
    if env:
        return Path(env)
    return Path(__file__).parent.parent.parent / "frontend" / "dist"


# Serve the built frontend at / when it exists. This must be the last route
# registered: routes match in registration order, so anything added after a
# mount at "/" would be unreachable — including, in an earlier revision, the
# other way around: a JSON "/" route registered first shadowed the mount and
# the built UI could never be served.
if _frontend_dist().exists():
    app.mount("/", StaticFiles(directory=_frontend_dist(), html=True), name="static")
else:

    @app.get("/")
    def root() -> dict[str, str]:
        """No built frontend: point at the API instead of 404ing."""
        return {"status": "ok", "version": "0.1.0", "docs": "/docs"}
