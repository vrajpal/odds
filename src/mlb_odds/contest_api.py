"""FastAPI app for the Circa Million consensus tool (C1: board + contest lines).

Run beside the MLB odds server: `uvicorn mlb_odds.contest_api:app`. Reads the
NFL odds database (read-only, same hardening rationale as api.py — the odds
file is collector-owned market history) and owns contest.sqlite for app state.
Both paths are deployment configuration, never request input.
"""

import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from mlb_odds import contest
from mlb_odds.storage import Storage

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Circa Million Consensus API",
    description="Weekly board: market spreads vs Circa contest lines",
)


def _resolve_nfl_db() -> Path:
    env = os.environ.get("NFL_ODDS_DB")
    return Path(env) if env else Path("./nfl-odds.sqlite")


def _resolve_contest_db() -> Path:
    env = os.environ.get("CONTEST_DB")
    return Path(env) if env else Path("./contest.sqlite")


def _open_odds() -> Storage:
    """NFL market data, read-only: this app must never create or migrate the
    collector's database (see Storage.__init__ / api.py `_resolve_db`)."""
    try:
        return Storage(_resolve_nfl_db(), read_only=True)
    except sqlite3.OperationalError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "NFL odds database unavailable. "
                "Run `mlb-odds collect --once --sport nfl` to create it."
            ),
        ) from exc


class ContestLineIn(BaseModel):
    week: int = Field(ge=1, le=contest.NUM_WEEKS)
    game_id: str
    home_spread: float

    @field_validator("home_spread")
    @classmethod
    def _plausible_half_point(cls, v: float) -> float:
        if abs(v) > 30:
            raise ValueError(f"implausible contest spread {v}")
        if (v * 2) != int(v * 2):
            raise ValueError(f"contest spread must be a multiple of 0.5, got {v}")
        return v


class ContestLineOut(BaseModel):
    week: int
    game_id: str
    home_spread: float
    entered_at: str


class BoardGameOut(BaseModel):
    game_id: str
    away_team: str
    home_team: str
    start_time: str  # Pacific ISO — the contest's timezone
    books: dict[str, float]
    consensus: float | None
    contest_line: float | None
    line_entered_at: str | None
    edge: float | None  # contest_line - consensus; positive → value on home
    value_side: str | None
    key_numbers: list[float]
    movement_since_entry: float | None


class BoardOut(BaseModel):
    week: int
    window_start: str
    window_end: str
    lines_post: str
    deadline: str
    seconds_to_deadline: int
    locked: bool  # past the week's Saturday 4 PM PT deadline
    games: list[BoardGameOut]


def _pt(value: datetime) -> str:
    return value.astimezone(contest.PACIFIC).isoformat()


@app.get("/api/contest/board", response_model=BoardOut)
def get_board(week: int | None = None) -> BoardOut:
    """The weekly board: market context + contest lines + countdown.

    `week` defaults to the contest week containing now; outside the season a
    week must be passed explicitly.
    """
    now = datetime.now(UTC)
    if week is None:
        week = contest.week_of(now)
        if week is None:
            raise HTTPException(
                status_code=400,
                detail="Now is outside contest Weeks 1-18; pass ?week= explicitly.",
            )
    if not 1 <= week <= contest.NUM_WEEKS:
        raise HTTPException(status_code=422, detail=f"week must be 1-{contest.NUM_WEEKS}")

    odds = _open_odds()
    store = contest.ContestStore(_resolve_contest_db())
    try:
        rows = contest.build_board(odds, store.lines(week), week)
    finally:
        store.close()
        odds.close()

    start, end = contest.week_window(week)
    deadline = contest.pick_deadline(week)
    return BoardOut(
        week=week,
        window_start=_pt(start),
        window_end=_pt(end),
        lines_post=_pt(contest.lines_post_time(week)),
        deadline=_pt(deadline),
        seconds_to_deadline=int((deadline - now).total_seconds()),
        locked=now >= deadline,
        games=[
            BoardGameOut(
                game_id=row.game_id,
                away_team=row.away_team,
                home_team=row.home_team,
                start_time=_pt(row.start_time),
                books=row.books,
                consensus=row.consensus,
                contest_line=row.contest_line,
                line_entered_at=(
                    _pt(row.line_entered_at) if row.line_entered_at is not None else None
                ),
                edge=row.edge,
                value_side=row.value_side,
                key_numbers=row.key_numbers,
                movement_since_entry=row.movement_since_entry,
            )
            for row in sorted(rows, key=lambda r: (r.start_time, r.game_id))
        ],
    )


@app.post("/api/contest/lines", response_model=ContestLineOut, status_code=201)
def set_contest_line(body: ContestLineIn) -> ContestLineOut:
    """Enter (or correct) one manually read Circa contest line.

    The game must already exist in the odds database inside that contest
    week's window — rejecting unknown ids here keeps a typo from becoming a
    phantom row that never joins to the board.
    """
    odds = _open_odds()
    try:
        known = {g.game_id for g in odds.games(window=contest.week_window(body.week))}
    finally:
        odds.close()
    if body.game_id not in known:
        raise HTTPException(
            status_code=404,
            detail=(
                f"{body.game_id} is not a stored NFL game in contest week {body.week}. "
                "Collect NFL odds first; the board shows valid game_ids."
            ),
        )

    entered_at = datetime.now(UTC)
    store = contest.ContestStore(_resolve_contest_db())
    try:
        store.set_line(body.week, body.game_id, body.home_spread, entered_at=entered_at)
    finally:
        store.close()
    logger.info(
        "contest line set: week %d %s home %+.1f", body.week, body.game_id, body.home_spread
    )
    return ContestLineOut(
        week=body.week,
        game_id=body.game_id,
        home_spread=body.home_spread,
        entered_at=_pt(entered_at),
    )


@app.get("/api/contest/lines", response_model=list[ContestLineOut])
def get_contest_lines(week: int) -> list[ContestLineOut]:
    """All entered contest lines for a week."""
    if not 1 <= week <= contest.NUM_WEEKS:
        raise HTTPException(status_code=422, detail=f"week must be 1-{contest.NUM_WEEKS}")
    store = contest.ContestStore(_resolve_contest_db())
    try:
        lines = store.lines(week)
    finally:
        store.close()
    return [
        ContestLineOut(
            week=line.week,
            game_id=line.game_id,
            home_spread=line.home_spread,
            entered_at=_pt(line.entered_at),
        )
        for line in sorted(lines.values(), key=lambda li: li.game_id)
    ]


@app.get("/api/contest/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
