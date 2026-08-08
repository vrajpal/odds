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

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from mlb_odds.models import Game
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
    # C2/C3: the consensus workflow. proposals are blind until a member's own
    # set is submitted (proposal_submissions marks that); votes are a member's
    # latest stance on a contested game and override their proposal; cards are
    # the locked five picks (contest rule: one submission, no changes), with
    # per-pick results entered after grading.
    """
    CREATE TABLE proposal_submissions (
        week         INTEGER NOT NULL CHECK (week BETWEEN 1 AND 18),
        member       TEXT NOT NULL,
        submitted_at TEXT NOT NULL,
        PRIMARY KEY (week, member)
    );

    CREATE TABLE proposals (
        week       INTEGER NOT NULL,
        member     TEXT NOT NULL,
        game_id    TEXT NOT NULL,
        side       TEXT NOT NULL CHECK (side IN ('home', 'away')),
        note       TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (week, member, game_id)
    );

    CREATE TABLE votes (
        week     INTEGER NOT NULL,
        member   TEXT NOT NULL,
        game_id  TEXT NOT NULL,
        side     TEXT NOT NULL CHECK (side IN ('home', 'away')),
        cast_at  TEXT NOT NULL,
        PRIMARY KEY (week, member, game_id)
    );

    CREATE TABLE cards (
        week      INTEGER PRIMARY KEY CHECK (week BETWEEN 1 AND 18),
        locked_by TEXT NOT NULL,
        locked_at TEXT NOT NULL,
        etsn      TEXT
    );

    CREATE TABLE card_picks (
        week    INTEGER NOT NULL REFERENCES cards(week),
        game_id TEXT NOT NULL,
        side    TEXT NOT NULL CHECK (side IN ('home', 'away')),
        result  TEXT CHECK (result IN ('win', 'loss', 'push')),
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

    # -- C2: proposals (blind), votes, cards --

    def submit_proposals(
        self,
        week: int,
        member: str,
        picks: Sequence[tuple[str, str, str]],  # (game_id, side, note)
        *,
        submitted_at: datetime,
    ) -> None:
        """One blind submission per member per week, 1-5 picks, immutable once
        in — the blind phase only works if nobody can edit after peeking."""
        if not 1 <= len(picks) <= 5:
            raise ValueError(f"propose 1-5 picks, got {len(picks)}")
        games = [g for g, _s, _n in picks]
        if len(set(games)) != len(games):
            raise ValueError("duplicate game in proposal set")
        if self.has_submitted(week, member):
            raise ValueError(f"{member} already submitted proposals for week {week}")
        with self._conn:
            self._conn.execute(
                "INSERT INTO proposal_submissions (week, member, submitted_at)"
                " VALUES (?, ?, ?)",
                (week, member, _to_utc_iso(submitted_at)),
            )
            self._conn.executemany(
                "INSERT INTO proposals (week, member, game_id, side, note)"
                " VALUES (?, ?, ?, ?, ?)",
                [(week, member, g, s, n) for g, s, n in picks],
            )

    def has_submitted(self, week: int, member: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM proposal_submissions WHERE week = ? AND member = ?",
                (week, member),
            ).fetchone()
            is not None
        )

    def submitted_members(self, week: int) -> list[str]:
        return [
            row[0]
            for row in self._conn.execute(
                "SELECT member FROM proposal_submissions WHERE week = ?"
                " ORDER BY submitted_at",
                (week,),
            )
        ]

    def proposals(self, week: int, *, member: str | None = None) -> list[Proposal]:
        sql = "SELECT week, member, game_id, side, note FROM proposals WHERE week = ?"
        params: tuple[object, ...] = (week,)
        if member is not None:
            sql += " AND member = ?"
            params = (week, member)
        return [
            Proposal(week=w, member=m, game_id=g, side=s, note=n)
            for w, m, g, s, n in self._conn.execute(sql + " ORDER BY member, game_id", params)
        ]

    def cast_vote(
        self, week: int, member: str, game_id: str, side: str, *, cast_at: datetime
    ) -> None:
        """A member's latest stance on one game; re-voting replaces."""
        if side not in ("home", "away"):
            raise ValueError(f"side must be home/away, got {side!r}")
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO votes (week, member, game_id, side, cast_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (week, member, game_id) DO UPDATE
                    SET side = excluded.side, cast_at = excluded.cast_at
                """,
                (week, member, game_id, side, _to_utc_iso(cast_at)),
            )

    def votes(self, week: int) -> list[Vote]:
        return [
            Vote(week=w, member=m, game_id=g, side=s)
            for w, m, g, s in self._conn.execute(
                "SELECT week, member, game_id, side FROM votes WHERE week = ?"
                " ORDER BY member, game_id",
                (week,),
            )
        ]

    def lock_card(
        self,
        week: int,
        picks: Sequence[tuple[str, str]],  # (game_id, side)
        *,
        locked_by: str,
        locked_at: datetime,
    ) -> None:
        """The week's official five. Exactly 5 distinct games, one card per
        week — mirroring the contest's one-submission rule."""
        if len(picks) != 5:
            raise ValueError(f"a card is exactly 5 picks, got {len(picks)}")
        games = [g for g, _s in picks]
        if len(set(games)) != 5:
            raise ValueError("duplicate game on card")
        if self.card(week) is not None:
            raise ValueError(f"week {week} card is already locked")
        with self._conn:
            self._conn.execute(
                "INSERT INTO cards (week, locked_by, locked_at) VALUES (?, ?, ?)",
                (week, locked_by, _to_utc_iso(locked_at)),
            )
            self._conn.executemany(
                "INSERT INTO card_picks (week, game_id, side) VALUES (?, ?, ?)",
                [(week, g, s) for g, s in picks],
            )

    def card(self, week: int) -> Card | None:
        row = self._conn.execute(
            "SELECT locked_by, locked_at, etsn FROM cards WHERE week = ?", (week,)
        ).fetchone()
        if row is None:
            return None
        picks = tuple(
            CardPick(game_id=g, side=s, result=r)
            for g, s, r in self._conn.execute(
                "SELECT game_id, side, result FROM card_picks WHERE week = ?"
                " ORDER BY game_id",
                (week,),
            )
        )
        return Card(
            week=week,
            picks=picks,
            locked_by=row[0],
            locked_at=datetime.fromisoformat(row[1]),
            etsn=row[2],
        )

    def set_etsn(self, week: int, etsn: str) -> None:
        with self._conn:
            updated = self._conn.execute(
                "UPDATE cards SET etsn = ? WHERE week = ?", (etsn, week)
            ).rowcount
        if updated == 0:
            raise ValueError(f"no locked card for week {week}")

    # -- C3: grading --

    def record_results(self, week: int, results: dict[str, str]) -> None:
        """Enter (or correct) grading for card picks: game_id -> win/loss/push."""
        card = self.card(week)
        if card is None:
            raise ValueError(f"no locked card for week {week}")
        on_card = {p.game_id for p in card.picks}
        unknown = set(results) - on_card
        if unknown:
            raise ValueError(f"not on the week {week} card: {sorted(unknown)}")
        bad = {r for r in results.values() if r not in ("win", "loss", "push")}
        if bad:
            raise ValueError(f"results must be win/loss/push, got {sorted(bad)}")
        with self._conn:
            self._conn.executemany(
                "UPDATE card_picks SET result = ? WHERE week = ? AND game_id = ?",
                [(r, week, g) for g, r in results.items()],
            )

    def all_cards(self) -> list[Card]:
        weeks = [
            row[0] for row in self._conn.execute("SELECT week FROM cards ORDER BY week")
        ]
        return [c for w in weeks if (c := self.card(w)) is not None]

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


# --- C2: consensus workflow -------------------------------------------------


def captain_for(week: int, members: Sequence[str]) -> str:
    """Weekly captain by fixed rotation: deadlock-breaker and submitter of
    record. Rotation is positional in the configured member order."""
    if not members:
        raise ValueError("no contest members configured")
    return members[(week - 1) % len(members)]


def effective_deadline(week: int, kickoffs: Iterable[datetime]) -> datetime:
    """Rule 8: a card containing any game that kicks off before the Saturday
    4 PM PT deadline is due in full before the earliest such kickoff."""
    base = pick_deadline(week)
    early = [k for k in kickoffs if k < base]
    return min(early) if early else base


@dataclass(frozen=True)
class Proposal:
    week: int
    member: str
    game_id: str
    side: str
    note: str


@dataclass(frozen=True)
class Vote:
    week: int
    member: str
    game_id: str
    side: str


@dataclass(frozen=True)
class Candidate:
    """One (game, side) under consideration, with everyone's current stance.

    A member's vote on a game overrides their proposal on it — a stance is a
    single opinion, revisable until the card locks. `status`:
    - "unanimous": every member backs it → auto-locked into the working card
    - "majority":  strictly more than half back it
    - "contested": anything else → the week's captain decides (Rule via group
      agreement, not contest rules)
    """

    game_id: str
    side: str
    backers: tuple[str, ...]
    status: str


def tally_candidates(
    proposals: Sequence[Proposal],
    votes: Sequence[Vote],
    members: Sequence[str],
) -> list[Candidate]:
    """Fold proposals + votes into per-(game, side) candidates.

    Ordered by backer count (desc) then game_id/side, so the top of the list
    is the working card."""
    stance: dict[tuple[str, str], str] = {}  # (member, game_id) -> side
    for p in proposals:
        stance[(p.member, p.game_id)] = p.side
    for v in votes:  # later, and overriding
        stance[(v.member, v.game_id)] = v.side

    backers: dict[tuple[str, str], list[str]] = {}
    for (member, game_id), side in stance.items():
        backers.setdefault((game_id, side), []).append(member)

    candidates = []
    for (game_id, side), who in backers.items():
        who_ordered = tuple(m for m in members if m in who)
        if len(who_ordered) == len(members):
            status = "unanimous"
        elif len(who_ordered) * 2 > len(members):
            status = "majority"
        else:
            status = "contested"
        candidates.append(
            Candidate(game_id=game_id, side=side, backers=who_ordered, status=status)
        )
    candidates.sort(key=lambda c: (-len(c.backers), c.game_id, c.side))
    return candidates


@dataclass(frozen=True)
class CardPick:
    game_id: str
    side: str
    result: str | None = None  # win | loss | push, entered after grading

    @property
    def points(self) -> float | None:
        if self.result is None:
            return None
        return {"win": 1.0, "push": 0.5, "loss": 0.0}[self.result]


@dataclass(frozen=True)
class Card:
    week: int
    picks: tuple[CardPick, ...]
    locked_by: str
    locked_at: datetime
    etsn: str | None  # Circa's 12-digit electronic ticket serial number


# --- C3: season scoring -----------------------------------------------------

QUARTER_WEEKS: dict[int, tuple[int, int]] = {1: (1, 4), 2: (5, 9), 3: (10, 13), 4: (14, 18)}


@dataclass(frozen=True)
class WeekScore:
    week: int
    wins: int
    losses: int
    pushes: int
    graded: int
    picks: int

    @property
    def points(self) -> float:
        return self.wins + 0.5 * self.pushes

    @property
    def complete(self) -> bool:
        return self.picks == 5 and self.graded == 5


def week_score(card: Card) -> WeekScore:
    results = [p.result for p in card.picks]
    return WeekScore(
        week=card.week,
        wins=results.count("win"),
        losses=results.count("loss"),
        pushes=results.count("push"),
        graded=sum(1 for r in results if r is not None),
        picks=len(card.picks),
    )


@dataclass(frozen=True)
class TiebreakerLadder:
    """The full-season 1st-place tiebreaker chain (rules 14c), in order."""

    total_wins: int
    winning_weeks: int  # weeks scoring > 2.5 points
    weeks_5_0: int
    weeks_4_0_1: int
    weeks_4_1: int


def season_summary(
    cards: Sequence[Card], *, now: datetime
) -> tuple[list[WeekScore], TiebreakerLadder, dict[int, float], bool]:
    """(week scores, tiebreaker ladder, quarter points, booby eligibility).

    Booby prizes require five picks in every *completed* contest week — a week
    whose window has fully elapsed with no locked 5-pick card disqualifies
    permanently (rules 14, "5 selections in all completed Contest Weeks").
    """
    by_week = {c.week: c for c in cards}
    scores = [week_score(c) for c in sorted(cards, key=lambda c: c.week)]

    ladder = TiebreakerLadder(
        total_wins=sum(s.wins for s in scores),
        winning_weeks=sum(1 for s in scores if s.points > 2.5),
        weeks_5_0=sum(1 for s in scores if s.wins == 5),
        weeks_4_0_1=sum(1 for s in scores if s.wins == 4 and s.pushes == 1),
        weeks_4_1=sum(1 for s in scores if s.wins == 4 and s.losses == 1),
    )

    quarters = {
        q: sum(s.points for s in scores if lo <= s.week <= hi)
        for q, (lo, hi) in QUARTER_WEEKS.items()
    }

    booby_eligible = True
    for week in range(1, NUM_WEEKS + 1):
        _, end = week_window(week)
        if end > now:
            break
        card = by_week.get(week)
        if card is None or len(card.picks) != 5:
            booby_eligible = False
            break

    return scores, ladder, quarters, booby_eligible


def booby_guard_alert(week: int, *, card_locked: bool, now: datetime) -> bool:
    """True once the group is inside the danger window: it is this contest
    week, Saturday 10:00 AM PT has passed, and no card is locked."""
    if card_locked or week_of(now) != week:
        return False
    deadline = pick_deadline(week)
    guard = datetime.combine(deadline.date(), time(10, 0), tzinfo=PACIFIC)
    return now >= guard


def grade_pick(side: str, home_spread: float, home_score: int, away_score: int) -> str:
    """Grade one ATS pick against the Circa contest line (rules 6/19).

    The contest number is static, so grading is pure arithmetic: home covers
    when (home margin + home spread) > 0, lands exactly on the number -> push
    (half point), and the away side is the mirror. Forfeits are the one case
    this can't see (NFL-awarded W/L without a score); grade those by hand.
    """
    if side not in ("home", "away"):
        raise ValueError(f"side must be home/away, got {side!r}")
    margin = (home_score - away_score) + home_spread
    if margin == 0:
        return "push"
    home_covered = margin > 0
    return "win" if (side == "home") == home_covered else "loss"


# --- C4.2-C4.5: decision-support stats ---------------------------------------


def pick_side_value(side: str, contest_line: float, market: float) -> float:
    """Points of value the taken side gets versus a market number: positive
    means the contest number is better than `market` for that side. With
    market = current consensus this is the pick's edge; with market = closing
    consensus it is the pick's CLV."""
    if side not in ("home", "away"):
        raise ValueError(f"side must be home/away, got {side!r}")
    diff = contest_line - market
    return round(diff if side == "home" else -diff, 2)


@dataclass(frozen=True)
class ClvRow:
    week: int
    game_id: str
    side: str
    contest_line: float
    closing: float | None  # consensus as of kickoff; None = no pre-start snapshot
    clv: float | None
    result: str | None


def clv_report(odds: Storage, store: ContestStore) -> list[ClvRow]:
    """Closing line value for every locked pick with a contest line (C4.2).

    CLV converges long before win rate does at 90 picks/season: it is the
    north-star metric for whether the process beats the market."""
    rows: list[ClvRow] = []
    for card in store.all_cards():
        lines = store.lines(card.week)
        games = {g.game_id: g for g in odds.games(window=week_window(card.week))}
        for pick in card.picks:
            line = lines.get(pick.game_id)
            game = games.get(pick.game_id)
            if line is None or game is None:
                continue
            ticks = spread_history(odds, pick.game_id)
            closing = consensus(book_spreads(ticks, asof=game.start_time))
            clv = (
                pick_side_value(pick.side, line.home_spread, closing)
                if closing is not None
                else None
            )
            rows.append(
                ClvRow(
                    week=card.week,
                    game_id=pick.game_id,
                    side=pick.side,
                    contest_line=line.home_spread,
                    closing=closing,
                    clv=clv,
                    result=pick.result,
                )
            )
    return rows


@dataclass(frozen=True)
class CalibrationBucket:
    label: str
    n: int
    wins: int
    losses: int
    pushes: int

    @property
    def cover_rate(self) -> float | None:
        decided = self.wins + self.losses
        return round(self.wins / decided, 3) if decided else None


def calibration_report(odds: Storage, store: ContestStore) -> list[CalibrationBucket]:
    """Cover rate by at-lock edge bucket, plus a key-number-crossing bucket
    (C4.3). Edge is side-adjusted: positive = we took the market-value side.
    The `< 0` bucket is the honest one — picks made against the edge signal."""
    tallies: dict[str, list[int]] = {
        "edge < 0": [0, 0, 0],
        "0 <= edge < 1": [0, 0, 0],
        "1 <= edge < 2": [0, 0, 0],
        "edge >= 2": [0, 0, 0],
        "key number crossed": [0, 0, 0],
    }
    slot = {"win": 0, "loss": 1, "push": 2}
    for card in store.all_cards():
        lines = store.lines(card.week)
        for pick in card.picks:
            line = lines.get(pick.game_id)
            if pick.result is None or line is None:
                continue
            ticks = spread_history(odds, pick.game_id)
            at_lock = consensus(book_spreads(ticks, asof=card.locked_at))
            if at_lock is None:
                continue
            edge = pick_side_value(pick.side, line.home_spread, at_lock)
            if edge < 0:
                label = "edge < 0"
            elif edge < 1:
                label = "0 <= edge < 1"
            elif edge < 2:
                label = "1 <= edge < 2"
            else:
                label = "edge >= 2"
            tallies[label][slot[pick.result]] += 1
            if key_numbers_crossed(line.home_spread, at_lock):
                tallies["key number crossed"][slot[pick.result]] += 1
    return [
        CalibrationBucket(label=label, n=sum(t), wins=t[0], losses=t[1], pushes=t[2])
        for label, t in tallies.items()
    ]


def power_ratings(odds: Storage) -> tuple[dict[str, float], float] | None:
    """Market-implied team ratings + home-field advantage (C4.4).

    Each game with a market spread contributes one equation
    r_home - r_away + hfa = -spread (predicted home margin); ridge-regularized
    least squares over every stored game keeps early-season fits sane. Uses
    the closing consensus for started games and the latest otherwise, so the
    fit follows the market's most-informed number per game.
    """
    import numpy as np

    games = odds.games()
    rows: list[tuple[str, str, float]] = []
    for game in games:
        ticks = spread_history(odds, game.game_id)
        market = consensus(book_spreads(ticks, asof=game.start_time))
        if market is None:
            market = consensus(book_spreads(ticks))
        if market is not None:
            rows.append((game.home_team, game.away_team, -market))
    if len(rows) < 8:  # not enough equations for 32 unknowns to mean anything
        return None
    teams_sorted = sorted({t for h, a, _ in rows for t in (h, a)})
    index = {t: i for i, t in enumerate(teams_sorted)}
    n = len(teams_sorted)
    x = np.zeros((len(rows), n + 1))
    y = np.zeros(len(rows))
    for i, (home, away, margin) in enumerate(rows):
        x[i, index[home]] = 1.0
        x[i, index[away]] = -1.0
        x[i, n] = 1.0  # hfa column
        y[i] = margin
    lam = 1.0  # ridge: shrinks ratings toward 0 when a team has few lines
    a = x.T @ x + lam * np.eye(n + 1)
    b = x.T @ y
    solution = np.linalg.solve(a, b)
    ratings_raw = solution[:n]
    ratings_raw = ratings_raw - ratings_raw.mean()  # identifiable: mean 0
    ratings = {t: round(float(r), 2) for t, r in zip(teams_sorted, ratings_raw, strict=True)}
    return ratings, round(float(solution[n]), 2)


def predicted_home_spread(
    ratings: dict[str, float], hfa: float, home: str, away: str
) -> float | None:
    """The model's home spread: negative = home favored (same convention as
    every spread in this package)."""
    if home not in ratings or away not in ratings:
        return None
    return round(-(ratings[home] - ratings[away] + hfa), 1)


@dataclass(frozen=True)
class GameContext:
    """Situational flags (C4.5), derived purely from the stored schedule."""

    home_rest: int | None  # days since the team's previous stored game
    away_rest: int | None
    divisional: bool

    @property
    def rest_differential(self) -> int | None:
        """home_rest - away_rest: positive = home team is better rested.

        None until both teams have a stored prior game (e.g. week 1). The
        handicapping-relevant magnitudes are 3+ (bye or Thursday mini-bye vs
        a normal week); +/-1 is ordinary schedule jitter."""
        if self.home_rest is None or self.away_rest is None:
            return None
        return self.home_rest - self.away_rest


def rest_days(all_games: Sequence[Game], team: str, before: datetime) -> int | None:
    """Calendar days (Pacific) between the team's previous stored game and
    `before` — the NFL convention: Sunday -> next Sunday is 7, Sunday ->
    Thursday is 4. Counting floor(24h) periods on the UTC instants instead
    would undercount any late-kickoff -> earlier-kickoff gap (SNF -> next
    Sunday 1:25 is 6d17h), and UTC dates would push night games onto the
    wrong day entirely (SNF kicks off past midnight UTC)."""
    prior = [
        g.start_time
        for g in all_games
        if team in (g.home_team, g.away_team) and g.start_time < before
    ]
    if not prior:
        return None
    last = max(prior)
    return (before.astimezone(PACIFIC).date() - last.astimezone(PACIFIC).date()).days


def game_context(all_games: Sequence[Game], game: Game) -> GameContext:
    from mlb_odds.teams import NFL_DIVISIONS

    return GameContext(
        home_rest=rest_days(all_games, game.home_team, game.start_time),
        away_rest=rest_days(all_games, game.away_team, game.start_time),
        divisional=(
            NFL_DIVISIONS.get(game.home_team) is not None
            and NFL_DIVISIONS.get(game.home_team) == NFL_DIVISIONS.get(game.away_team)
        ),
    )


@dataclass(frozen=True)
class MemberStats:
    member: str
    proposal_wins: int
    proposal_losses: int
    proposal_pushes: int
    stance_wins: int
    stance_losses: int
    stance_pushes: int
    captain_weeks: int
    captain_points: float


def _mirror(result: str) -> str:
    return {"win": "loss", "loss": "win", "push": "push"}[result]


def member_stats(store: ContestStore, members: Sequence[str]) -> list[MemberStats]:
    """Per-member records over graded card picks (C4.5).

    A pick graded on the card's side also grades every member's *stance* on
    that game: same side → same result, opposite side → mirrored (the line is
    identical, so covering is symmetric). Proposal record counts only games
    the member originally proposed; stance record uses their final position
    (vote overriding proposal). Captain record: the week's points under each
    member's captaincy.
    """
    counts: dict[str, dict[str, int]] = {
        m: {"pw": 0, "pl": 0, "pp": 0, "sw": 0, "sl": 0, "sp": 0, "cw": 0}
        for m in members
    }
    captain_points: dict[str, float] = {m: 0.0 for m in members}
    for card in store.all_cards():
        graded = {p.game_id: p for p in card.picks if p.result is not None}
        captain = captain_for(card.week, members)
        if captain in counts:
            counts[captain]["cw"] += 1
            captain_points[captain] += week_score(card).points
        proposals = store.proposals(card.week)
        stances: dict[tuple[str, str], str] = {}
        for p in proposals:
            stances[(p.member, p.game_id)] = p.side
        for v in store.votes(card.week):
            stances[(v.member, v.game_id)] = v.side
        for p in proposals:
            pick = graded.get(p.game_id)
            if pick is None or p.member not in counts:
                continue
            outcome = pick.result or "push"
            result = outcome if p.side == pick.side else _mirror(outcome)
            counts[p.member]["p" + result[0]] += 1
        for (member, game_id), side in stances.items():
            pick = graded.get(game_id)
            if pick is None or member not in counts:
                continue
            outcome = pick.result or "push"
            result = outcome if side == pick.side else _mirror(outcome)
            counts[member]["s" + result[0]] += 1
    return [
        MemberStats(
            member=m,
            proposal_wins=counts[m]["pw"],
            proposal_losses=counts[m]["pl"],
            proposal_pushes=counts[m]["pp"],
            stance_wins=counts[m]["sw"],
            stance_losses=counts[m]["sl"],
            stance_pushes=counts[m]["sp"],
            captain_weeks=counts[m]["cw"],
            captain_points=round(captain_points[m], 1),
        )
        for m in members
    ]
