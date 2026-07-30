"""Typer CLI, entrypoint `mlb-odds`. See SPEC FR5.

All timestamps are stored UTC and converted to the local timezone here, at the
display layer only.
"""

import logging
import os
from datetime import date, datetime, tzinfo
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from mlb_odds import collector
from mlb_odds.client import OddsClient
from mlb_odds.models import GameOdds, Market, Quote
from mlb_odds.providers import ProviderError, TheOddsAPI

app = typer.Typer(
    help="Fetch, normalize, and store MLB betting odds.",
    no_args_is_help=True,
    add_completion=False,
)

@app.callback()
def _load_env() -> None:
    # CLI layer only — importing mlb_odds as a library never reads .env files
    # (docs/DECISIONS.md D-011). Real environment variables take precedence.
    # Explicit path: bare load_dotenv() searches the *module's* tree, not the cwd.
    load_dotenv(Path(".env"))


DbOption = Annotated[
    Path | None,
    typer.Option("--db", help="SQLite path (default: $MLB_ODDS_DB or ./odds.sqlite)."),
]


class ExportFormat(StrEnum):
    csv = "csv"
    parquet = "parquet"


def _resolve_db(db: Path | None) -> Path:
    if db is not None:
        return db
    env = os.environ.get("MLB_ODDS_DB")
    return Path(env) if env else Path("./odds.sqlite")


def _local_tz() -> tzinfo:
    tz = datetime.now().astimezone().tzinfo
    assert tz is not None  # astimezone() always fills tzinfo
    return tz


@app.command()
def collect(
    once: Annotated[
        bool, typer.Option("--once", help="One fetch cycle, then exit (cron-friendly).")
    ] = False,
    interval: Annotated[
        float,
        typer.Option(min=1, help="Seconds between polls. Mind the quota math (see README)."),
    ] = 300.0,
    db: DbOption = None,
) -> None:
    """Poll providers and append odds snapshots to the database."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        provider = TheOddsAPI()
    except ProviderError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from None
    client = OddsClient(providers=[provider], db=_resolve_db(db))
    try:
        collector.run(client, interval, once=once)
    finally:
        client.close()


@app.command()
def today(db: DbOption = None) -> None:
    """Show today's games with the latest moneyline / run line / total per book.

    Reads stored data only — no network calls, no API credits.
    """
    client = OddsClient(providers=[], db=_resolve_db(db))
    try:
        tz = _local_tz()
        today_local = datetime.now(tz).date()
        board = [
            go
            for go in client.current_odds()
            if go.game.start_time.astimezone(tz).date() == today_local
        ]
        if not board:
            typer.echo("No stored odds for today. Run `mlb-odds collect --once` first.")
            return
        _render_board(board, tz)
    finally:
        client.close()


def _render_board(board: list[GameOdds], tz: tzinfo) -> None:
    by_game: dict[str, list[GameOdds]] = {}
    for go in board:
        by_game.setdefault(go.game.game_id, []).append(go)
    for game_id in sorted(by_game, key=lambda gid: by_game[gid][0].game.start_time):
        entries = by_game[game_id]
        game = entries[0].game
        start_local = game.start_time.astimezone(tz)
        typer.echo(
            f"{game.away_team} @ {game.home_team}  "
            f"{start_local:%Y-%m-%d %I:%M %p %Z}  [{game.game_id}]"
        )
        typer.echo(f"  {'book':<18}{'moneyline':<14}{'run line':<16}{'total':<14}")
        for go in entries:
            for book in sorted({q.book for q in go.quotes}):
                quotes = [q for q in go.quotes if q.book == book]
                typer.echo(
                    f"  {book:<18}"
                    f"{_fmt_moneyline(quotes):<14}"
                    f"{_fmt_run_line(quotes):<16}"
                    f"{_fmt_total(quotes):<14}"
                )
        typer.echo("")


def _find(quotes: list[Quote], market: Market, outcome: str) -> Quote | None:
    return next((q for q in quotes if q.market == market and q.outcome == outcome), None)


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


@app.command()
def closing(
    on: Annotated[
        str | None,
        typer.Option("--date", help="YYYY-MM-DD (UTC) to limit the board to one slate."),
    ] = None,
    db: DbOption = None,
) -> None:
    """Show closing lines: the last stored snapshot at or before first pitch.

    Reads stored data only — no network calls, no API credits. A game appears
    once at least one pre-start snapshot exists; collect close to first pitch
    for a closing line worth the name.
    """
    client = OddsClient(providers=[], db=_resolve_db(db))
    try:
        board = client.closing_odds(date.fromisoformat(on) if on else None)
        if not board:
            typer.echo("No closing lines stored" + (f" for {on}" if on else "") + ".")
            return
        _render_board(board, _local_tz())
    finally:
        client.close()


@app.command()
def history(
    game: Annotated[
        str, typer.Argument(help="Canonical game_id (e.g. 2026-07-09-NYM-NYY-1) or AWAY@HOME.")
    ],
    on: Annotated[
        str | None,
        typer.Option("--date", help="YYYY-MM-DD (UTC) to disambiguate the AWAY@HOME form."),
    ] = None,
    db: DbOption = None,
) -> None:
    """Show line movement for one game, one row per (fetched_at, book, market, outcome)."""
    client = OddsClient(providers=[], db=_resolve_db(db))
    try:
        game_id = _resolve_game_id(client, game, on)
        df = client.history_df(game_id)
        if df.empty:
            typer.echo(f"No stored odds for {game_id}.", err=True)
            raise typer.Exit(1)
        df["fetched_at"] = df["fetched_at"].dt.tz_convert(_local_tz())
        typer.echo(f"{game_id} — {len(df)} rows")
        typer.echo(df.to_string(index=False))
    finally:
        client.close()


def _resolve_game_id(client: OddsClient, game: str, on: str | None) -> str:
    """Accept a canonical game_id as-is, or resolve fuzzy AWAY@HOME [+ --date]."""
    if "@" not in game:
        return game
    try:
        away, home = (part.strip().upper() for part in game.split("@"))
    except ValueError:
        typer.echo(f"error: expected AWAY@HOME, got {game!r}", err=True)
        raise typer.Exit(2) from None
    on_date = date.fromisoformat(on) if on else None
    matches = [
        g
        for g in client.games(on_date)
        if g.away_team == away and g.home_team == home
    ]
    if not matches:
        typer.echo(f"error: no stored game matching {away}@{home}"
                   f"{f' on {on}' if on else ''}.", err=True)
        raise typer.Exit(1)
    if len(matches) > 1:
        typer.echo(f"error: {away}@{home} is ambiguous; pass a full game_id:", err=True)
        for g in matches:
            typer.echo(f"  {g.game_id}", err=True)
        raise typer.Exit(2)
    return matches[0].game_id


@app.command()
def export(
    out: Annotated[Path, typer.Option("--out", help="Output file path.")],
    fmt: Annotated[
        ExportFormat, typer.Option("--format", help="Output format.")
    ] = ExportFormat.csv,
    db: DbOption = None,
) -> None:
    """Dump all stored odds (joined with game context) to CSV or Parquet."""
    client = OddsClient(providers=[], db=_resolve_db(db))
    try:
        df = client.odds_df()
        if fmt is ExportFormat.csv:
            df.to_csv(out, index=False)
        else:
            try:
                df.to_parquet(out, index=False)
            except ImportError:
                typer.echo(
                    "error: parquet export needs a parquet engine — pip install pyarrow",
                    err=True,
                )
                raise typer.Exit(1) from None
        typer.echo(f"wrote {len(df)} rows to {out}")
    finally:
        client.close()


if __name__ == "__main__":
    app()
