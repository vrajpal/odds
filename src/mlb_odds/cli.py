"""Typer CLI, entrypoint `mlb-odds`. See SPEC FR5.

All timestamps are stored UTC and converted to the local timezone here, at the
display layer only.
"""

import logging
import os
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import time as dt_time
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from mlb_odds import collector
from mlb_odds import projections as projections_mod
from mlb_odds import statcast as statcast_mod
from mlb_odds.client import OddsClient
from mlb_odds.models import PROP_MARKETS_BY_SPORT, Game, GameOdds, Market, Quote
from mlb_odds.providers import ESPN, OddsProvider, ProviderError, TheOddsAPI

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
    typer.Option(
        "--db",
        help="SQLite path (default: $MLB_ODDS_DB / $NFL_ODDS_DB per sport, else "
        "./odds.sqlite for mlb, ./nfl-odds.sqlite for nfl).",
    ),
]


class ExportFormat(StrEnum):
    csv = "csv"
    parquet = "parquet"


class ProviderChoice(StrEnum):
    the_odds_api = "the_odds_api"
    espn = "espn"
    all = "all"


class SportChoice(StrEnum):
    mlb = "mlb"
    nfl = "nfl"


def _build_providers(
    choice: ProviderChoice, sport: SportChoice, bookmakers: list[str] | None = None
) -> list[OddsProvider]:
    """Construct the chosen providers. TheOddsAPI() raises ProviderError without
    a key; ESPN needs none, so `--provider espn` collects on a bare machine.
    `bookmakers` selects named books on The Odds API (D-027); ESPN is a single
    book and ignores it."""
    if choice is ProviderChoice.espn:
        return [ESPN(sport=sport.value)]
    if choice is ProviderChoice.the_odds_api:
        return [TheOddsAPI(sport=sport.value, bookmakers=bookmakers)]
    return [TheOddsAPI(sport=sport.value, bookmakers=bookmakers), ESPN(sport=sport.value)]


# One database per sport: MLB KC and NFL KC would otherwise collide on the
# same canonical game_id date-away-home-number scheme (D-019).
_DB_ENV = {SportChoice.mlb: "MLB_ODDS_DB", SportChoice.nfl: "NFL_ODDS_DB"}
_DB_DEFAULT = {SportChoice.mlb: "./odds.sqlite", SportChoice.nfl: "./nfl-odds.sqlite"}


def _resolve_db(db: Path | None, sport: SportChoice = SportChoice.mlb) -> Path:
    if db is not None:
        return db
    env = os.environ.get(_DB_ENV[sport])
    return Path(env) if env else Path(_DB_DEFAULT[sport])


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
    changed_only: Annotated[
        bool,
        typer.Option(
            "--changed-only",
            help="Append only quotes that differ from the newest stored row. "
            "History then records changes, not polls.",
        ),
    ] = False,
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Poll only while a stored game is in its live window "
            "(first pitch -15m to +4h); idle otherwise. Mind the quota math.",
        ),
    ] = False,
    provider: Annotated[
        ProviderChoice,
        typer.Option(
            "--provider",
            help="Odds source(s): the_odds_api (metered), espn (free, one book), or all.",
        ),
    ] = ProviderChoice.the_odds_api,
    sport: Annotated[
        SportChoice,
        typer.Option("--sport", help="League: mlb (default) or nfl."),
    ] = SportChoice.mlb,
    bookmaker: Annotated[
        list[str] | None,
        typer.Option(
            "--bookmaker",
            help="Poll named books instead of the us region (repeatable, max 10 "
            "for unchanged cost — D-027). e.g. --bookmaker pinnacle",
        ),
    ] = None,
    db: DbOption = None,
) -> None:
    """Poll providers and append odds snapshots to the database."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if once and live:
        typer.echo("error: --once and --live are mutually exclusive", err=True)
        raise typer.Exit(2)
    try:
        providers = _build_providers(provider, sport, bookmaker)
    except ProviderError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from None
    client = OddsClient(
        providers=providers, db=_resolve_db(db, sport), changed_only=changed_only
    )
    try:
        collector.run(client, interval, once=once, live=live)
    finally:
        client.close()


@app.command()
def props(
    market: Annotated[
        list[str],
        typer.Option(
            "--market",
            help="Prop market key (repeatable). MLB: batter_home_runs, batter_hits, "
            "batter_total_bases, pitcher_strikeouts. NFL: player_pass_yds, "
            "player_pass_tds, player_rush_yds, player_receptions.",
        ),
    ],
    changed_only: Annotated[
        bool,
        typer.Option("--changed-only", help="Append only prop prices that moved (D-015)."),
    ] = False,
    sport: Annotated[
        SportChoice,
        typer.Option("--sport", help="League: mlb (default) or nfl."),
    ] = SportChoice.mlb,
    db: DbOption = None,
) -> None:
    """Fetch player-prop ladders for today's events, one snapshot per run.

    Metered: each event costs up to [markets] x [regions] credits, so a full
    slate at two markets can spend ~30 credits. There is deliberately no loop
    mode — cron this like `collect --once` if you want history.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    supported = PROP_MARKETS_BY_SPORT[sport.value]
    bad = [m for m in market if m not in supported]
    if bad:
        typer.echo(
            f"error: unsupported {sport.value} prop market(s) {bad};"
            f" choose from {list(supported)}",
            err=True,
        )
        raise typer.Exit(2)
    try:
        provider = TheOddsAPI(sport=sport.value)
    except ProviderError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from None
    client = OddsClient(
        providers=[provider], db=_resolve_db(db, sport), changed_only=changed_only
    )
    try:
        results = client.fetch_and_store_props(market)
        rows = sum(len(go.quotes) for go in results)
        typer.echo(
            f"{len(results)} games, {rows} prop rows; "
            f"credits remaining: {provider.quota_remaining}"
        )
    except ProviderError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        client.close()


@app.command()
def results(
    on: Annotated[
        str | None,
        typer.Option(
            "--date",
            help="YYYY-MM-DD scoreboard day (US/Eastern, how ESPN groups days). "
            "Default: every day with a stored game started >3h ago and no score.",
        ),
    ] = None,
    sport: Annotated[
        SportChoice,
        typer.Option("--sport", help="League: mlb (default) or nfl."),
    ] = SportChoice.mlb,
    db: DbOption = None,
) -> None:
    """Store final scores for completed games (ESPN scoreboard — free, D-024).

    Cron this after slates finish; re-runs are safe (corrections overwrite,
    unfinished games are skipped and picked up next run).
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    from mlb_odds.providers.espn import SCOREBOARD_DAY_TZ

    client = OddsClient(providers=[], db=_resolve_db(db, sport))
    try:
        # Only games that plausibly finished: started 3+ hours ago.
        cutoff = datetime.now(UTC) - timedelta(hours=3)
        if on is not None:
            days = [date.fromisoformat(on)]
            cutoff = datetime.now(UTC)  # explicit day: trust the operator
        else:
            pending = client.games_missing_results(before=cutoff)
            days = sorted(
                {g.start_time.astimezone(SCOREBOARD_DAY_TZ).date() for g in pending}
            )
            if not days:
                typer.echo("No completed games are missing scores.")
                return
        recorded = client.fetch_and_store_results(
            ESPN(sport=sport.value), days, before=cutoff
        )
        still_missing = len(
            client.games_missing_results(before=datetime.now(UTC) - timedelta(hours=3))
        )
        typer.echo(f"{recorded} final score(s) recorded; {still_missing} still missing.")
    finally:
        client.close()


@app.command()
def projections(
    csv_file: Annotated[
        Path | None, typer.Argument(help="Exported projections CSV.")
    ] = None,
    fetch: Annotated[
        bool,
        typer.Option(
            "--fetch",
            help="Pull today's MLB batter projections live from FanDuel "
            "Research instead of reading a CSV (no auth needed).",
        ),
    ] = False,
    sport: Annotated[
        SportChoice,
        typer.Option("--sport", help="League: mlb (default) or nfl."),
    ] = SportChoice.mlb,
    source: Annotated[
        str, typer.Option("--source", help="Projection source tag.")
    ] = projections_mod.DEFAULT_SOURCE,
    db: DbOption = None,
) -> None:
    """Import projections (D-037): a FanDuel Research CSV export, or --fetch
    to pull today's MLB slate live. Every import appends a timestamped
    snapshot — history is the accuracy ledger, so import daily (before first
    pitch) rather than only on game day.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    from mlb_odds.storage import SAME_GAME_START_TOLERANCE, Storage

    if fetch == (csv_file is not None):
        typer.echo("error: provide a CSV file or --fetch (exactly one)", err=True)
        raise typer.Exit(2)
    if fetch:
        if sport is not SportChoice.mlb:
            typer.echo("error: --fetch supports mlb only", err=True)
            raise typer.Exit(2)
        from mlb_odds.providers.base import ProviderError
        from mlb_odds.providers.fanduel_research import FanDuelResearch

        provider = FanDuelResearch()
        try:
            rows = projections_mod.aggregate_players(provider.fetch_mlb_batters())
        except (ProviderError, projections_mod.ProjectionParseError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from None
        finally:
            provider.close()
    else:
        assert csv_file is not None
        try:
            rows = projections_mod.parse_csv(csv_file.read_text(), sport.value)
        except (OSError, projections_mod.ProjectionParseError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from None

    storage = Storage(_resolve_db(db, sport))
    try:
        if fetch:
            # The Odds API only lists a game once books post lines (often
            # mid-morning, pitcher-dependent), but ESPN's schedule is up at
            # dawn — store it so a pre-lines fetch has games to attach to
            # (same ids the odds poll will reuse). Best-effort: stored games
            # still match if ESPN is down.
            try:
                stored = storage.store_games(
                    ESPN(sport="mlb").fetch_schedule(datetime.now(_local_tz()).date())
                )
                typer.echo(f"{stored} schedule rows stored (ESPN)")
            except ProviderError as exc:
                typer.echo(f"warning: ESPN schedule fetch failed ({exc}); "
                           "matching already-stored games only", err=True)
        now = datetime.now(UTC)
        # Candidate games: anything upcoming or recent enough to matter.
        candidates: dict[tuple[str, str], list[Game]] = {}
        for g in storage.games():
            if g.start_time >= now - timedelta(days=2):
                candidates.setdefault((g.away_team, g.home_team), []).append(g)
        matched = started = unknown = 0
        for row in rows:
            games_for = candidates.get((row.away_team, row.home_team), [])
            upcoming = [g for g in games_for
                        if g.start_time >= now - SAME_GAME_START_TOLERANCE]
            if not upcoming:
                label = f"{row.away_team} @ {row.home_team}"
                if games_for:
                    started += 1
                    typer.echo(f"  skipped {label}: game already underway "
                               "(a post-start snapshot could never score)")
                else:
                    unknown += 1
                    typer.echo(f"  skipped {label}: no stored game "
                               "(has the odds poll run?)")
                continue
            game = min(upcoming, key=lambda g: g.start_time)
            storage.add_projection(
                game.game_id, source, fetched_at=now,
                home_win_prob=row.home_win_prob,
                away_score=row.away_score, home_score=row.home_score,
            )
            matched += 1
        typer.echo(
            f"{matched}/{len(rows)} projections matched to stored games "
            f"(source: {source})."
        )
        if started or unknown:
            typer.echo(f"  ({started} already underway, {unknown} not stored "
                       "— import before first pitch to feed the ledger)")
    finally:
        storage.close()


@app.command()
def statcast(
    on: Annotated[
        str | None,
        typer.Option("--date", help="Schedule date YYYY-MM-DD (default: today local)."),
    ] = None,
    db: DbOption = None,
) -> None:
    """Fetch the scouting layer (D-031): schedule (ESPN), probable starters
    (MLB Stats API), and Savant expected stats. All free; MLB only.

    Cron-able daily; re-runs upsert (probables firm up as game day nears).
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    from mlb_odds.providers.espn import SCOREBOARD_DAY_TZ
    from mlb_odds.storage import SAME_GAME_START_TOLERANCE, Storage

    target = date.fromisoformat(on) if on else datetime.now(_local_tz()).date()
    storage = Storage(_resolve_db(db, SportChoice.mlb))
    try:
        espn = ESPN(sport="mlb")
        stored_games = storage.store_games(espn.fetch_schedule(target))

        source = statcast_mod.StatcastSource()
        day_start = datetime.combine(target, dt_time(0, 0), tzinfo=SCOREBOARD_DAY_TZ)
        window = (day_start, day_start + timedelta(days=1))
        by_matchup: dict[tuple[str, str], list[Game]] = {}
        for g in storage.games(window=window):
            by_matchup.setdefault((g.away_team, g.home_team), []).append(g)
        now = datetime.now(UTC)
        matched = 0
        probables = source.probables(target)
        for pg in probables:
            candidates = [
                g for g in by_matchup.get((pg.away_team, pg.home_team), [])
                if abs(g.start_time - pg.start_time) <= SAME_GAME_START_TOLERANCE
            ]
            if not candidates:
                continue
            game = min(candidates, key=lambda g: abs(g.start_time - pg.start_time))
            storage.upsert_probables(
                game.game_id, pg.away_pitcher, pg.home_pitcher, fetched_at=now
            )
            matched += 1

        season = target.year
        team_rows = source.team_expected(season)
        storage.upsert_statcast_team(
            [(t.team, t.season, t.pa, t.xba, t.xslg, t.xwoba) for t in team_rows],
            fetched_at=now,
        )
        pitcher_rows = source.pitcher_expected(season)
        storage.upsert_statcast_pitcher(
            [(p.name, p.season, p.pa, p.xba, p.xslg, p.xwoba, p.xera) for p in pitcher_rows],
            fetched_at=now,
        )
        typer.echo(
            f"{target}: {stored_games} schedule rows stored, "
            f"{matched}/{len(probables)} probables matched, "
            f"{len(team_rows)} team + {len(pitcher_rows)} pitcher statcast lines."
        )
    except ProviderError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        storage.close()


@app.command()
def today(
    sport: Annotated[
        SportChoice,
        typer.Option("--sport", help="League: mlb (default) or nfl."),
    ] = SportChoice.mlb,
    db: DbOption = None,
) -> None:
    """Show today's games with the latest moneyline / run line / total per book.

    Reads stored data only — no network calls, no API credits.
    """
    client = OddsClient(providers=[], db=_resolve_db(db, sport))
    try:
        tz = _local_tz()
        today_local = datetime.now(tz).date()
        board = [
            go
            for go in client.current_odds()
            if go.game.start_time.astimezone(tz).date() == today_local
        ]
        if not board:
            sport_flag = "" if sport is SportChoice.mlb else f" --sport {sport.value}"
            typer.echo(
                f"No stored odds for today. Run `mlb-odds collect --once{sport_flag}` first."
            )
            return
        _render_board(board, tz, sport)
    finally:
        client.close()


def _render_board(
    board: list[GameOdds], tz: tzinfo, sport: "SportChoice | None" = None
) -> None:
    spread_market: Market = "spread" if sport is SportChoice.nfl else "run_line"
    spread_label = "spread" if sport is SportChoice.nfl else "run line"
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
        typer.echo(f"  {'book':<18}{'moneyline':<14}{spread_label:<16}{'total':<14}")
        for go in entries:
            for book in sorted({q.book for q in go.quotes}):
                quotes = [q for q in go.quotes if q.book == book]
                typer.echo(
                    f"  {book:<18}"
                    f"{_fmt_moneyline(quotes):<14}"
                    f"{_fmt_spread(quotes, spread_market):<16}"
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


def _fmt_spread(quotes: list[Quote], market: Market = "run_line") -> str:
    home = _find(quotes, market, "home")
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
    sport: Annotated[
        SportChoice,
        typer.Option("--sport", help="League: mlb (default) or nfl."),
    ] = SportChoice.mlb,
    db: DbOption = None,
) -> None:
    """Show closing lines: the last stored snapshot at or before first pitch.

    Reads stored data only — no network calls, no API credits. A game appears
    once at least one pre-start snapshot exists; collect close to first pitch
    for a closing line worth the name.
    """
    client = OddsClient(providers=[], db=_resolve_db(db, sport))
    try:
        board = client.closing_odds(date.fromisoformat(on) if on else None)
        if not board:
            typer.echo("No closing lines stored" + (f" for {on}" if on else "") + ".")
            return
        _render_board(board, _local_tz(), sport)
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
    sport: Annotated[
        SportChoice,
        typer.Option("--sport", help="League: mlb (default) or nfl."),
    ] = SportChoice.mlb,
    db: DbOption = None,
) -> None:
    """Show line movement for one game, one row per (fetched_at, book, market, outcome)."""
    client = OddsClient(providers=[], db=_resolve_db(db, sport))
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
    sport: Annotated[
        SportChoice,
        typer.Option("--sport", help="League: mlb (default) or nfl."),
    ] = SportChoice.mlb,
    db: DbOption = None,
) -> None:
    """Dump all stored odds (joined with game context) to CSV or Parquet."""
    client = OddsClient(providers=[], db=_resolve_db(db, sport))
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
