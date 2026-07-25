"""FastAPI server for MLB odds — REST API and static frontend."""

import logging
import os
from datetime import datetime, tzinfo
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mlb_odds.client import OddsClient
from mlb_odds.models import Quote

logger = logging.getLogger(__name__)

app = FastAPI(title="MLB Odds API", description="REST API for MLB betting odds")


def _resolve_db(db_path: str | None = None) -> Path:
    if db_path:
        return Path(db_path)
    env = os.environ.get("MLB_ODDS_DB")
    return Path(env) if env else Path("./odds.sqlite")


def _local_tz() -> tzinfo:
    tz = datetime.now().astimezone().tzinfo
    assert tz is not None
    return tz


def _get_client(db_path: str | None = None) -> OddsClient:
    return OddsClient(providers=[], db=_resolve_db(db_path))


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
def get_today(db: str | None = None) -> list[GameBoard]:
    """Get today's games with latest odds per book."""
    client = _get_client(db)
    try:
        tz = _local_tz()
        today_local = datetime.now(tz).date()
        board = [
            go
            for go in client.current_odds()
            if go.game.start_time.astimezone(tz).date() == today_local
        ]
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
def get_game_history(game_id: str, db: str | None = None) -> dict[str, object]:
    """Get line movement history for a game."""
    client = _get_client(db)
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
def export_odds(fmt: str = "csv", db: str | None = None) -> dict[str, object]:
    """Export all odds to CSV or JSON."""
    if fmt not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'json'")

    client = _get_client(db)
    try:
        df = client.odds_df()
        if df.empty:
            raise HTTPException(status_code=404, detail="No odds stored")

        if fmt == "json":
            return {"data": df.to_dict(orient="records"), "count": len(df)}

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


@app.get("/")
def root() -> dict[str, str]:
    """API health check."""
    return {"status": "ok", "version": "0.1.0"}


# Mount static frontend files if they exist
frontend_path = Path(__file__).parent.parent.parent / "frontend" / "dist"
if frontend_path.exists():
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="static")
