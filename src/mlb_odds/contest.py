"""Circa Million VIII consensus tool: contest calendar, contest lines, edge math.

The contest (see circa-million-2026-rules.md) grades five weekly picks against
Circa's *static* contest spreads. The market context for judging those spreads
comes from this repo's NFL odds database; the contest lines themselves are
contest-only numbers that exist on no feed and are entered by hand.

Conventions in this module:
- Every spread is the HOME team's line (negative = home favored), matching the
  board rendering everywhere else in this package.
- `edge = contest_line - market consensus`. Positive edge means the market
  rates home better than Circa's number charges, so HOME at the contest line
  is the value side; negative edge is value on AWAY. (Example: contest -2.5,
  market -4.5 → edge +2.0 → home covers a number 2 points softer than market.)
- All contest scheduling is Pacific wall-clock time (the rules' timezone).
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from mlb_odds.storage import Storage

PACIFIC = ZoneInfo("America/Los_Angeles")

# Rule 19: a "Contest Week" is Wednesday 2:00 AM PT through the following
# Wednesday 1:59 AM PT — half-open [Wed 2:00, next Wed 2:00). Week 1's
# Wednesday precedes the 2026 NFL opener (Thu Sep 10); Week 18 ends at the
# contest's stated end instant, 1:59 AM Wed Jan 13, 2027.
WEEK1_WEDNESDAY = date(2026, 9, 9)
NUM_WEEKS = 18
_WEEK_START = time(2, 0)
_DEADLINE_TIME = time(16, 0)  # Rule 7: selections due 4:00 PM PT Saturday
_LINES_POST_TIME = time(10, 0)  # Rule 7: spreads post ~10:00 AM PT Thursday

# Holiday slates get a Wednesday line post instead of Thursday (Rule 7a names
# Thanksgiving; the contest page adds Christmas week). 2026 season dates.
EARLY_POST_HOLIDAYS = (date(2026, 11, 26), date(2026, 12, 25))

# Football key numbers: margins 3 and 7 are far likelier than neighbors, so a
# contest line and the market sitting on opposite sides of one is worth more
# than the raw point difference suggests.
KEY_NUMBERS = (3.0, 7.0)


def week_window(week: int) -> tuple[datetime, datetime]:
    """Half-open [start, end) instants of a contest week, Pacific wall clock.

    Both bounds are built as wall-clock 2:00 AM rather than start + 7 days:
    the week containing the November DST fall-back is 169 hours long, and the
    rules speak in local time, not durations.
    """
    if not 1 <= week <= NUM_WEEKS:
        raise ValueError(f"contest week must be 1-{NUM_WEEKS}, got {week}")
    start_day = WEEK1_WEDNESDAY + timedelta(weeks=week - 1)
    start = datetime.combine(start_day, _WEEK_START, tzinfo=PACIFIC)
    end = datetime.combine(start_day + timedelta(days=7), _WEEK_START, tzinfo=PACIFIC)
    return start, end


def week_of(at: datetime) -> int | None:
    """Contest week containing instant `at`, or None outside Weeks 1-18."""
    if at.tzinfo is None:
        raise ValueError("week_of requires an aware datetime")
    for week in range(1, NUM_WEEKS + 1):
        start, end = week_window(week)
        if start <= at < end:
            return week
    return None


def pick_deadline(week: int) -> datetime:
    """Rule 7: 4:00 PM PT on the contest week's Saturday.

    This is the card-level *latest* deadline. Rule 8 (any earlier-kicking
    selected game pulls the whole card's deadline to that kickoff) is a
    property of a proposed card, not of the week — it lands with C2.
    """
    start, _ = week_window(week)
    return datetime.combine(start.date() + timedelta(days=3), _DEADLINE_TIME, tzinfo=PACIFIC)


def lines_post_time(week: int) -> datetime:
    """When Circa posts the week's contest spreads: Thursday 10:00 AM PT,
    or Wednesday 10:00 AM PT for the Thanksgiving/Christmas weeks."""
    start, _ = week_window(week)
    week_dates = {start.date() + timedelta(days=n) for n in range(7)}
    early = any(holiday in week_dates for holiday in EARLY_POST_HOLIDAYS)
    post_day = start.date() if early else start.date() + timedelta(days=1)
    return datetime.combine(post_day, _LINES_POST_TIME, tzinfo=PACIFIC)


def _validate_spread(home_spread: float) -> None:
    """Contest spreads are half-point-quantized and plausible-sized. Catching a
    fat-fingered -35 or -3.25 at entry beats discovering it on the board."""
    if abs(home_spread) > 30:
        raise ValueError(f"implausible contest spread {home_spread}")
    if (home_spread * 2) != int(home_spread * 2):
        raise ValueError(f"contest spread must be a multiple of 0.5, got {home_spread}")


@dataclass(frozen=True)
class ContestLine:
    game_id: str
    week: int
    home_spread: float
    entered_at: datetime  # UTC; anchors "movement since entry"


CONTEST_MIGRATIONS: list[str] = [
    """
    CREATE TABLE contest_lines (
        week        INTEGER NOT NULL CHECK (week BETWEEN 1 AND 18),
        game_id     TEXT NOT NULL,
        home_spread REAL NOT NULL,
        entered_at  TEXT NOT NULL,
        PRIMARY KEY (week, game_id)
    );
    """,
]


class ContestStore:
    """App state for the consensus tool, in its own SQLite file.

    Deliberately not the odds database: that file is an append-only record of
    what the market said, written only by the collector. Contest lines are
    human-entered and correctable (typos happen), so they get their own file
    with upsert semantics. Same migration discipline as storage.py.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        current = row[0] if row else 0
        for version, script in enumerate(CONTEST_MIGRATIONS[current:], start=current + 1):
            self._conn.executescript(script)
            self._conn.execute("DELETE FROM schema_version")
            self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            self._conn.commit()

    def set_line(
        self, week: int, game_id: str, home_spread: float, *, entered_at: datetime
    ) -> None:
        """Upsert one contest line. Re-entry overwrites (typo correction) and
        re-anchors entered_at — movement is measured from the latest entry."""
        if not 1 <= week <= NUM_WEEKS:
            raise ValueError(f"contest week must be 1-{NUM_WEEKS}, got {week}")
        _validate_spread(home_spread)
        if entered_at.tzinfo is None:
            raise ValueError("entered_at must be timezone-aware")
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO contest_lines (week, game_id, home_spread, entered_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (week, game_id) DO UPDATE
                    SET home_spread = excluded.home_spread,
                        entered_at = excluded.entered_at
                """,
                (week, game_id, home_spread, _to_utc_iso(entered_at)),
            )

    def lines(self, week: int) -> dict[str, ContestLine]:
        """Contest lines for one week, keyed by game_id."""
        rows = self._conn.execute(
            "SELECT game_id, home_spread, entered_at FROM contest_lines WHERE week = ?",
            (week,),
        ).fetchall()
        return {
            game_id: ContestLine(
                game_id=game_id,
                week=week,
                home_spread=home_spread,
                entered_at=datetime.fromisoformat(entered_at),
            )
            for game_id, home_spread, entered_at in rows
        }

    def close(self) -> None:
        self._conn.close()


def _to_utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class SpreadTick:
    """One stored home-spread observation for a game."""

    fetched_at: datetime
    provider: str
    book: str
    home_spread: float


def spread_history(odds: Storage, game_id: str) -> list[SpreadTick]:
    """Every stored home-side spread row for a game, oldest first.

    Only market='spread' home rows — run_line (baseball) and prop rows never
    enter contest math. This is the raw material for both "latest consensus"
    and "consensus as of the moment the contest line was entered".
    """
    ticks = [
        SpreadTick(
            fetched_at=datetime.fromisoformat(fetched_at),
            provider=provider,
            book=book,
            home_spread=line,
        )
        for fetched_at, provider, book, market, outcome, line, price, player in
        odds.history_rows(game_id)
        if market == "spread" and outcome == "home" and player is None and line is not None
    ]
    ticks.sort(key=lambda t: (t.fetched_at, t.provider, t.book))
    return ticks


def book_spreads(ticks: list[SpreadTick], asof: datetime | None = None) -> dict[str, float]:
    """Newest home spread per book at-or-before `asof` (None = latest stored).

    Mirrors latest_odds semantics: a book that stops reporting keeps its
    last-known number rather than vanishing — under `--changed-only`
    collection, absence of newer rows *means* "unchanged", so carrying the
    last value forward is what makes movement math correct, not a fallback.
    The same book reported by two providers resolves to the newest row.
    """
    newest: dict[str, SpreadTick] = {}
    for tick in ticks:  # oldest-first, so later assignment = newer row wins
        if asof is not None and tick.fetched_at > asof:
            continue
        current = newest.get(tick.book)
        if current is None or tick.fetched_at >= current.fetched_at:
            newest[tick.book] = tick
    return {book: tick.home_spread for book, tick in newest.items()}


def consensus(spreads: dict[str, float]) -> float | None:
    """Market consensus home spread: the median across books. Median, not
    mean — one book hanging a stale or off-market number shouldn't drag the
    reference point the edge is computed against."""
    if not spreads:
        return None
    return float(median(spreads.values()))


def key_numbers_crossed(contest_line: float, market: float) -> list[float]:
    """Key numbers strictly between the contest line and the market consensus,
    sign-aware (±3, ±7). Landing exactly on a key number is not a cross."""
    lo, hi = sorted((contest_line, market))
    return [k for n in KEY_NUMBERS for k in (n, -n) if lo < k < hi]


@dataclass(frozen=True, eq=False)
class BoardGame:
    """One game's row on the weekly board."""

    game_id: str
    away_team: str
    home_team: str
    start_time: datetime
    books: dict[str, float]
    consensus: float | None
    contest_line: float | None
    line_entered_at: datetime | None
    edge: float | None  # contest_line - consensus; >0 → value on home
    value_side: str | None  # "home" | "away" | None
    key_numbers: list[float]
    movement_since_entry: float | None  # consensus now - consensus at entry


def build_board(odds: Storage, lines: dict[str, ContestLine], week: int) -> list[BoardGame]:
    """The weekly board: every stored game in the contest week's window, with
    latest market spreads, contest line (when entered), and the derived edge.

    Games without an entered contest line still appear — the board is also how
    lines get entered, so it must show what's missing.
    """
    rows: list[BoardGame] = []
    for game in odds.games(window=week_window(week)):
        ticks = spread_history(odds, game.game_id)
        latest = book_spreads(ticks)
        market = consensus(latest)
        line = lines.get(game.game_id)

        edge = value_side = None
        keys: list[float] = []
        movement = None
        if line is not None and market is not None:
            edge = round(line.home_spread - market, 2)
            if edge > 0:
                value_side = "home"
            elif edge < 0:
                value_side = "away"
            keys = key_numbers_crossed(line.home_spread, market)
            at_entry = consensus(book_spreads(ticks, asof=line.entered_at))
            if at_entry is not None:
                movement = round(market - at_entry, 2)

        rows.append(
            BoardGame(
                game_id=game.game_id,
                away_team=game.away_team,
                home_team=game.home_team,
                start_time=game.start_time,
                books=latest,
                consensus=market,
                contest_line=line.home_spread if line else None,
                line_entered_at=line.entered_at if line else None,
                edge=edge,
                value_side=value_side,
                key_numbers=keys,
                movement_since_entry=movement,
            )
        )
    return rows
