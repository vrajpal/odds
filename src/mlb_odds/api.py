"""FastAPI server for MLB odds — REST API and static frontend."""

import logging
import os
import sqlite3
from datetime import datetime, time, timedelta, tzinfo
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mlb_odds.client import OddsClient
from mlb_odds.models import Quote

logger = logging.getLogger(__name__)

app = FastAPI(title="MLB Odds API", description="REST API for MLB betting odds")


def _resolve_db() -> Path:
    """The database path is deployment configuration, never request input.

    An earlier revision exposed this as a `db` query parameter on every
    endpoint. Because Storage opens read-write and migrates, that let any
    unauthenticated GET create a SQLite file at an arbitrary path, or add this
    schema to an unrelated SQLite database on the host. Server-side only.
    """
    env = os.environ.get("MLB_ODDS_DB")
    return Path(env) if env else Path("./odds.sqlite")


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


def _get_client() -> OddsClient:
    """Read-only, provider-less client.

    `providers=[]` is what keeps HTTP traffic from reaching The Odds API and
    burning metered credits; OddsClient enforces that read_only and providers
    are mutually exclusive so this can't regress silently.
    """
    try:
        return OddsClient(providers=[], db=_resolve_db(), read_only=True)
    except sqlite3.OperationalError as exc:
        raise HTTPException(
            status_code=503,
            detail="Odds database unavailable. Run `mlb-odds collect --once` to create it.",
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
def get_today() -> list[GameBoard]:
    """Get today's games with latest odds per book."""
    client = _get_client()
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
                result[game_id].books[book] = {
                    "moneyline": _fmt_moneyline(quotes),
                    "run_line": _fmt_run_line(quotes),
                    "total": _fmt_total(quotes),
                }

        return sorted(result.values(), key=lambda gb: gb.game.start_time)
    finally:
        client.close()


@app.get("/api/games/{game_id}/history")
def get_game_history(game_id: str) -> dict[str, object]:
    """Get line movement history for a game."""
    client = _get_client()
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
def export_odds(fmt: str = "csv") -> dict[str, object]:
    """Export all odds to CSV or JSON."""
    if fmt not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'json'")

    client = _get_client()
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


def _fmt_run_line(quotes: list[Quote]) -> str:
    home = _find(quotes, "run_line", "home")
    if home is None or home.line is None:
        return "-"
    return f"{home.line:+.1f} ({home.price:+d})"


def _fmt_total(quotes: list[Quote]) -> str:
    over = _find(quotes, "total", "over")
    if over is None or over.line is None:
        return "-"
    return f"{over.line:.1f} (o{over.price:+d})"


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
