"""FastAPI server for MLB/NFL odds — REST API and static frontend.

Every data endpoint takes `?sport=mlb|nfl` (default mlb) and reads that
sport's own database (D-019: one file per sport)."""

import logging
import math
import os
import sqlite3
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from time import monotonic
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mlb_odds import contest, matchup, model, projections, valuation
from mlb_odds.client import OddsClient
from mlb_odds.models import Quote, Sport
from mlb_odds.providers.base import ProviderError
from mlb_odds.providers.espn import ESPN
from mlb_odds.storage import Storage

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


def _local_day_window(tz: tzinfo, on: date | None = None) -> tuple[datetime, datetime]:
    """Half-open UTC [start, end) instants bounding one local day (default
    today).

    The board is a local-day view, but start_time is stored UTC, so a UTC
    calendar-date filter is the wrong question: a 10pm PDT first pitch is the
    next day in UTC and would drop out of the board entirely.
    """
    day = on or datetime.now(tz).date()
    start = datetime.combine(day, time.min, tzinfo=tz)
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


@app.get("/api/games/{game_id}/matchup")
def get_matchup(game_id: str, sport: Literal["mlb", "nfl"] = "mlb") -> dict[str, object]:
    """Head-to-head team lens for the matchup page (D-034): records,
    standings, and curated season stats for both teams from ESPN's free API,
    with per-row better-side comparison. Shared implementation: matchup.py."""
    try:
        game_date = date.fromisoformat(game_id[:10])
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"malformed game_id {game_id!r}") from exc
    try:
        storage = Storage(_resolve_db(sport), read_only=True)
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail="odds database unavailable") from exc
    try:
        game = next((g for g in storage.games(game_date) if g.game_id == game_id), None)
    finally:
        storage.close()
    if game is None:
        raise HTTPException(status_code=404, detail=f"unknown game {game_id}")
    try:
        payload = matchup.matchup_payload(
            ESPN(sport=sport), sport, game.away_team, game.home_team
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=f"ESPN fetch failed: {exc}") from exc
    if payload is None:
        raise HTTPException(
            status_code=502,
            detail=f"ESPN id missing for {game.away_team} or {game.home_team}",
        )
    return {"game_id": game_id, **payload}


@app.get("/api/games/{game_id}/scout")
def get_scout(game_id: str, sport: Literal["mlb", "nfl"] = "mlb") -> dict[str, object]:
    """Statcast matchup card (D-031): probable starters with expected-stats
    lines plus team batting expected stats. Fields are null until the daily
    statcast pull has run for that slate."""
    try:
        storage = Storage(_resolve_db(sport), read_only=True)
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail="odds database unavailable") from exc
    try:
        data = storage.scout(game_id)
    finally:
        storage.close()
    if data is None:
        raise HTTPException(status_code=404, detail=f"unknown game {game_id}")
    return {"game_id": game_id, **data}


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


def _scout_xwoba(scout_data: dict[str, object], key: str) -> float | None:
    section = scout_data.get(key)
    if isinstance(section, dict):
        value = section.get("xwoba")
        if isinstance(value, int | float):
            return float(value)
    return None


class BestPriceOut(BaseModel):
    book: str
    price: int
    ev: float  # vs the consensus fair probability; positive = value
    model_ev: float | None = None  # vs the MODEL's probability (D-036)


class MoneylineOut(BaseModel):
    consensus_prob: float | None  # devigged median home win probability
    open_prob: float | None  # consensus at the first stored snapshot
    drift: float | None  # consensus_prob - open_prob
    market_model_prob: float | None  # moneyline-implied strength model (D-030)
    statcast_prob: float | None  # Statcast-only term (D-032, MLB)
    spread_model_prob: float | None  # spread-ratings lens (D-036, NFL)
    projection_prob: float | None  # third-party projection lens (D-037)
    projection_source: str | None
    model_prob: float | None  # the sport's blend (D-032 MLB / D-036 NFL)
    model_edge: float | None  # model_prob - consensus_prob
    best_home: BestPriceOut | None
    best_away: BestPriceOut | None
    books: dict[str, dict[str, float | int]]  # book -> {home, away, prob}


class MarketQuoteOut(BaseModel):
    line: float | None
    home: int | None = None
    away: int | None = None
    over: int | None = None
    under: int | None = None


class DashboardGameOut(BaseModel):
    game_id: str
    away_team: str
    home_team: str
    start_time: str
    predicted_margin: float | None = None  # NFL: model home margin in points (D-036)
    moneyline: MoneylineOut
    run_line: dict[str, MarketQuoteOut]  # per book
    total: dict[str, MarketQuoteOut]


class StrengthOut(BaseModel):
    team: str
    strength: float  # log-odds vs league average; >0 = better than average


class DashboardOut(BaseModel):
    date: str
    sport: str
    hfa: float | None
    strengths: list[StrengthOut]  # best first; empty until enough games
    games: list[DashboardGameOut]


@app.get("/api/dashboard", response_model=DashboardOut)
def dashboard(sport: Literal["mlb", "nfl"] = "mlb", on: str | None = None) -> DashboardOut:
    """The betting dashboard (D-030): one local day's games with core
    markets per book, devigged consensus, the market-implied model, and the
    best-EV price on each side. `on` = YYYY-MM-DD, default today."""
    tz = _local_tz()
    try:
        target = date.fromisoformat(on) if on else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"bad date {on!r}") from exc
    try:
        storage = Storage(_resolve_db(sport), read_only=True)
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail="odds database unavailable") from exc
    try:
        window = _local_day_window(tz, target)
        games = storage.games(window=window)
        fitted = valuation.implied_strengths(storage)
        strengths, hfa = fitted if fitted else ({}, None)
        season = (target or datetime.now(tz).date()).year
        statcast_league = valuation.league_xwoba(storage.statcast_team_rows(season))
        spread_fit = contest.power_ratings(storage) if sport == "nfl" else None
        # latest_odds returns one GameOdds per (game, provider); merge quotes.
        merged: dict[str, list[Quote]] = {}
        for go in storage.latest_odds(window=window):
            merged.setdefault(go.game.game_id, []).extend(go.quotes)

        out_games = []
        for game in games:
            ticks = valuation.moneyline_history(storage, game.game_id)
            pairs = valuation.book_probs(ticks)
            fair = valuation.consensus_prob(pairs)
            open_prob = (
                valuation.consensus_prob(
                    valuation.book_probs(ticks, asof=ticks[0].fetched_at)
                )
                if ticks
                else None
            )
            market_model_prob = (
                valuation.model_home_prob(strengths, hfa, game.home_team, game.away_team)
                if hfa is not None
                else None
            )
            statcast_prob = None
            sc_logit = None
            if statcast_league is not None:
                scout_data = storage.scout(game.game_id) or {}
                sc_logit = valuation.statcast_home_logit(
                    home_batting=_scout_xwoba(scout_data, "home_batting"),
                    away_batting=_scout_xwoba(scout_data, "away_batting"),
                    home_starter_against=_scout_xwoba(scout_data, "home_pitcher_line"),
                    away_starter_against=_scout_xwoba(scout_data, "away_pitcher_line"),
                    league=statcast_league,
                    hfa_logit=hfa if hfa is not None else 0.07,
                )
                if sc_logit is not None:
                    statcast_prob = round(1.0 / (1.0 + math.exp(-sc_logit)), 4)
            proj = storage.latest_projection(game.game_id)
            proj_prob = None
            proj_source = None
            if proj is not None:
                proj_source = proj[0]
                proj_prob = projections.projection_prob(
                    proj[2], proj[3], proj[4], sport
                )
            spread_model_prob = None
            predicted_margin = None
            if sport == "nfl":
                predicted = None
                if spread_fit is not None:
                    predicted = contest.predicted_home_spread(
                        spread_fit[0], spread_fit[1], game.home_team, game.away_team
                    )
                if predicted is not None:
                    predicted_margin = round(-predicted, 1)
                model_prob, spread_model_prob = model.nfl_model_prob(
                    market_model_prob, predicted, proj_prob
                )
            else:
                model_prob = model.mlb_model_prob(
                    market_model_prob, sc_logit, proj_prob
                )
            best_home = best_away = None
            if fair is not None:
                bh, ba = valuation.best_prices(pairs, fair)
                if bh:
                    best_home = BestPriceOut(
                        book=bh.book, price=bh.price, ev=bh.ev,
                        model_ev=(
                            valuation.expected_value(model_prob, bh.price)
                            if model_prob is not None else None
                        ),
                    )
                if ba:
                    best_away = BestPriceOut(
                        book=ba.book, price=ba.price, ev=ba.ev,
                        model_ev=(
                            valuation.expected_value(1.0 - model_prob, ba.price)
                            if model_prob is not None else None
                        ),
                    )

            run_line: dict[str, MarketQuoteOut] = {}
            total: dict[str, MarketQuoteOut] = {}
            for q in merged.get(game.game_id, []):
                if q.market == "run_line" and q.line is not None:
                    entry = run_line.setdefault(q.book, MarketQuoteOut(line=None))
                    if q.outcome == "home":
                        entry.line = q.line
                        entry.home = q.price
                    elif q.outcome == "away":
                        entry.away = q.price
                elif q.market == "total" and q.line is not None:
                    entry = total.setdefault(q.book, MarketQuoteOut(line=q.line))
                    if q.outcome == "over":
                        entry.over = q.price
                    elif q.outcome == "under":
                        entry.under = q.price

            out_games.append(
                DashboardGameOut(
                    game_id=game.game_id,
                    away_team=game.away_team,
                    home_team=game.home_team,
                    start_time=game.start_time.astimezone(tz).isoformat(),
                    predicted_margin=predicted_margin,
                    moneyline=MoneylineOut(
                        consensus_prob=fair,
                        open_prob=open_prob,
                        market_model_prob=market_model_prob,
                        statcast_prob=statcast_prob,
                        spread_model_prob=spread_model_prob,
                        projection_prob=proj_prob,
                        projection_source=proj_source,
                        drift=(
                            round(fair - open_prob, 4)
                            if fair is not None and open_prob is not None
                            else None
                        ),
                        model_prob=model_prob,
                        model_edge=(
                            round(model_prob - fair, 4)
                            if model_prob is not None and fair is not None
                            else None
                        ),
                        best_home=best_home,
                        best_away=best_away,
                        books={
                            book: {
                                "home": t.home_price,
                                "away": t.away_price,
                                "prob": round(t.home_prob, 4),
                            }
                            for book, t in sorted(pairs.items())
                        },
                    ),
                    run_line=run_line,
                    total=total,
                )
            )
    finally:
        storage.close()
    day = target or datetime.now(tz).date()
    return DashboardOut(
        date=day.isoformat(),
        sport=sport,
        hfa=hfa,
        strengths=[
            StrengthOut(team=t, strength=v)
            for t, v in sorted(strengths.items(), key=lambda kv: -kv[1])
        ],
        games=sorted(out_games, key=lambda g: (g.start_time, g.game_id)),
    )


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


@app.get("/api/projections/report")
def projections_report(
    sport: Literal["mlb", "nfl"] = "mlb", source: str | None = None
) -> dict[str, object]:
    """The accuracy ledger (D-037): Brier score and hit rate of the latest
    pre-kickoff projection per finished game, against stored results. This is
    the evidence that will eventually set the projection lens's blend weight."""
    try:
        storage = Storage(_resolve_db(sport), read_only=True)
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail="odds database unavailable") from exc
    try:
        raw = storage.projection_outcomes(source=source)
    finally:
        storage.close()
    scored: list[tuple[float, int]] = []
    hits = 0
    for _gid, prob, p_away, p_home, home_score, away_score in raw:
        p = projections.projection_prob(prob, p_away, p_home, sport)
        if p is None:
            continue
        home_won = 1 if home_score > away_score else 0
        scored.append((p, home_won))
        if (p >= 0.5) == (home_won == 1):
            hits += 1
    return {
        "sport": sport,
        "source": source,
        "n": len(scored),
        "brier": projections.brier(scored),
        "hit_rate": round(hits / len(scored), 3) if scored else None,
        "note": "brier 0.25 = coin flip; lower is better",
    }


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
