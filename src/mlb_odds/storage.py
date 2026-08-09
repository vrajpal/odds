"""SQLite persistence. WAL mode, single writer, append-only odds snapshots.

Schema changes go through MIGRATIONS — append a script, never edit an applied one.
Keep the SQL portable (see docs/DECISIONS.md D-005).
"""

import logging
import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from mlb_odds.models import Game, GameOdds, Quote

logger = logging.getLogger("mlb_odds.storage")


def _utc_key(value: datetime) -> str:
    """Comparison key matching how start_time is stored.

    Stored timestamps are always UTC (models._require_utc), which is what makes
    lexical string comparison equal instant comparison. Normalizing here keeps
    that invariant when a caller hands us a datetime in some other zone.
    """
    if value.tzinfo is None:
        raise ValueError("window bounds must be timezone-aware")
    return value.astimezone(UTC).isoformat()

def _quote_key(
    book: str, market: str, outcome: str, player: str | None, line: float | None
) -> tuple[object, ...]:
    """changed_only identity: prop ladders key on (player, line) too (D-018)."""
    if player is None:
        return (book, market, outcome)
    return (book, market, outcome, player, line)


def _quote_value(player: str | None, line: float | None, price: int) -> tuple[object, ...]:
    """What counts as "changed": game markets compare (line, price); props
    carry the line in their key, so only price remains."""
    if player is None:
        return (line, price)
    return (price,)


# Widest plausible start-time disagreement for one physical game across providers
# or reschedules within the same day. Doubleheader halves are separated by at
# least game 1's duration (~2.5h+), so 2h cleanly splits "same game, times
# drifted" from "different half of a doubleheader" (see _resolve_game_id).
SAME_GAME_START_TOLERANCE = timedelta(hours=2)

MIGRATIONS: list[str] = [
    """
    CREATE TABLE games (
        game_id     TEXT PRIMARY KEY,
        start_time  TEXT NOT NULL,
        home_team   TEXT NOT NULL,
        away_team   TEXT NOT NULL,
        season      INTEGER NOT NULL
    );

    CREATE TABLE provider_game_ids (
        game_id     TEXT NOT NULL REFERENCES games(game_id),
        provider    TEXT NOT NULL,
        native_id   TEXT NOT NULL,
        PRIMARY KEY (provider, native_id)
    );

    CREATE TABLE odds (
        id          INTEGER PRIMARY KEY,
        game_id     TEXT NOT NULL REFERENCES games(game_id),
        fetched_at  TEXT NOT NULL,
        provider    TEXT NOT NULL,
        book        TEXT NOT NULL,
        market      TEXT NOT NULL,
        outcome     TEXT NOT NULL,
        line        REAL,
        price       INTEGER NOT NULL
    );

    CREATE INDEX idx_odds_game    ON odds (game_id, market, fetched_at);
    CREATE INDEX idx_odds_fetched ON odds (fetched_at);
    """,
    # Serves the window= range scan used by the local-day board (API /api/today).
    """
    CREATE INDEX idx_games_start ON games (start_time);
    """,
    # Player props (D-018): NULL for game markets, player name for prop rows.
    """
    ALTER TABLE odds ADD COLUMN player TEXT;
    """,
    # Final scores (D-024): row present == game is final; upsert corrects.
    """
    CREATE TABLE results (
        game_id     TEXT PRIMARY KEY REFERENCES games(game_id),
        home_score  INTEGER NOT NULL,
        away_score  INTEGER NOT NULL,
        fetched_at  TEXT NOT NULL
    );
    """,
    # Statcast scouting layer (D-031): probable starters per game and
    # expected-stats aggregates. Upserts everywhere — Savant re-fetches
    # supersede, probables change until first pitch.
    """
    CREATE TABLE probables (
        game_id      TEXT PRIMARY KEY REFERENCES games(game_id),
        away_pitcher TEXT,
        home_pitcher TEXT,
        fetched_at   TEXT NOT NULL
    );

    CREATE TABLE statcast_team (
        team       TEXT NOT NULL,
        season     INTEGER NOT NULL,
        pa         INTEGER NOT NULL,
        xba        REAL,
        xslg       REAL,
        xwoba      REAL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (team, season)
    );

    CREATE TABLE statcast_pitcher (
        name       TEXT NOT NULL,
        season     INTEGER NOT NULL,
        pa         INTEGER NOT NULL,
        xba        REAL,
        xslg       REAL,
        xwoba      REAL,
        xera       REAL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (name, season)
    );
    """,
]


class Storage:
    def __init__(self, db_path: str | Path, *, read_only: bool = False) -> None:
        """Open the database. `read_only=True` opens with SQLite's mode=ro URI:
        the file is never created, never migrated, never written.

        Readers that accept untrusted input (the HTTP API) must use it — a
        read-write open of an attacker-influenced path creates files and runs
        _migrate() against whatever it lands on. Only the CLI and collector,
        whose path comes from local configuration, open read-write.
        """
        self.read_only = read_only
        if read_only:
            # as_uri() gives an absolute, percent-encoded file: URI, so paths
            # containing ? or # can't smuggle extra URI parameters.
            uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True)
            self._conn.execute("PRAGMA foreign_keys=ON")
            return
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        current = row[0] if row else 0
        for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
            logger.info("applying schema migration %d", version)
            self._conn.executescript(script)
            self._conn.execute("DELETE FROM schema_version")
            self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            self._conn.commit()

    def store(self, results: list[GameOdds], *, changed_only: bool = False) -> int:
        """Persist one fetch cycle: upsert games, append quotes. Returns rows written.

        Game identity is reconciled against previously stored native ids first
        (see _resolve_game_id), and the models in `results` are updated in place
        to carry the canonical game_id.

        `changed_only=True` skips quotes identical (line, price) to the newest
        stored row for the same (game, provider, book, market, outcome), so an
        unchanged board appends nothing (D-015). History then records changes,
        not polls: a missing timestamp means "same as before", not "book gone" —
        latest_odds is unaffected since it already carries last-known quotes
        forward. Prop quotes come as ladders (one player, several lines), so
        their identity key includes player and line, and only price is compared.
        """
        written = 0
        with self._conn:
            for game_odds in results:
                game = game_odds.game
                game.game_id = self._resolve_game_id(game)
                quotes = game_odds.quotes
                if changed_only:
                    current = self._latest_quote_values(game.game_id, game_odds.provider)
                    quotes = [
                        q
                        for q in quotes
                        if current.get(_quote_key(q.book, q.market, q.outcome, q.player, q.line))
                        != _quote_value(q.player, q.line, q.price)
                    ]
                self._conn.execute(
                    """
                    INSERT INTO games (game_id, start_time, home_team, away_team, season)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (game_id) DO UPDATE SET start_time = excluded.start_time
                    """,
                    (
                        game.game_id,
                        game.start_time.isoformat(),
                        game.home_team,
                        game.away_team,
                        game.season,
                    ),
                )
                self._conn.executemany(
                    "INSERT OR IGNORE INTO provider_game_ids (game_id, provider, native_id)"
                    " VALUES (?, ?, ?)",
                    [(game.game_id, p, n) for p, n in game.provider_ids.items()],
                )
                self._conn.executemany(
                    """
                    INSERT INTO odds (game_id, fetched_at, provider, book, market, outcome,
                                      line, price, player)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            game.game_id,
                            game_odds.fetched_at.isoformat(),
                            game_odds.provider,
                            q.book,
                            q.market,
                            q.outcome,
                            q.line,
                            q.price,
                            q.player,
                        )
                        for q in quotes
                    ],
                )
                written += len(quotes)
        return written

    def _latest_quote_values(
        self, game_id: str, provider: str
    ) -> dict[tuple[object, ...], tuple[object, ...]]:
        """Newest stored values per quote identity for one (game, provider) —
        the comparison baseline for changed_only writes.

        Game markets: identity (book, market, outcome), value (line, price) —
        a line move is a change. Prop markets: identity includes player and
        line (ladders carry one row per rung), value is price alone.
        """
        rows = self._conn.execute(
            """
            SELECT o.book, o.market, o.outcome, o.line, o.price, o.player
            FROM odds AS o
            JOIN (
                SELECT book, market, outcome, player, line, MAX(fetched_at) AS fetched_at
                FROM odds
                WHERE game_id = ? AND provider = ?
                GROUP BY book, market, outcome,
                         COALESCE(player, ''),
                         CASE WHEN player IS NULL THEN 0 ELSE COALESCE(line, 0) END
            ) AS latest
              ON  latest.book = o.book
              AND latest.market = o.market
              AND latest.outcome = o.outcome
              AND COALESCE(latest.player, '') = COALESCE(o.player, '')
              AND (o.player IS NULL OR COALESCE(latest.line, 0) = COALESCE(o.line, 0))
              AND latest.fetched_at = o.fetched_at
            WHERE o.game_id = ? AND o.provider = ?
            ORDER BY o.id
            """,
            (game_id, provider, game_id, provider),
        ).fetchall()
        return {
            _quote_key(book, market, outcome, player, line): _quote_value(player, line, price)
            for book, market, outcome, line, price, player in rows
        }

    def _resolve_game_id(self, game: Game) -> str:
        """Canonical game_id for this game, stable across fetch cycles (FR2).

        A provider can only number doubleheaders from what its current response
        contains: once game 1 finishes and drops out of the feed, game 2 stands
        alone and would be numbered 1, silently merging two physical games. So:

        1. A native id we have already stored keeps its previously assigned
           game_id, whatever number the provider computed this cycle.
        2. A genuinely new game never takes a game_id that a *different* native
           id from the same provider has already claimed — its game_number is
           bumped until free. (A different provider's native id mapping to the
           same game_id is fine — that is cross-provider convergence, D-008.)
        3. Convergence onto an existing game_id (whoever stored it) also
           requires the stored start_time to be within SAME_GAME_START_TOLERANCE
           of the incoming one. A far-apart start time means the provisional
           game_number points at the other half of a doubleheader — e.g. a
           provider whose feed dropped a finished game 1 numbers game 2 as 1,
           which without this check would misfile its quotes under game 1's
           game_id when a different provider stored both halves (D-010).
        """
        for provider, native_id in game.provider_ids.items():
            row = self._conn.execute(
                "SELECT game_id FROM provider_game_ids WHERE provider = ? AND native_id = ?",
                (provider, native_id),
            ).fetchone()
            if row is not None:
                return str(row[0])
        # Converge onto any stored same-slate game whose start time matches,
        # regardless of the number this provider computed: a provider that saw
        # the doubleheader in the opposite order numbers the halves 2/1, and
        # bumping only upward from its own number could never reach the lower
        # canonical id — the early half would split off as a phantom game 3.
        game_id = game.game_id
        base, _, _ = game_id.rpartition("-")
        candidates = self._conn.execute(
            "SELECT game_id, start_time FROM games WHERE game_id LIKE ?",
            (f"{base}-%",),
        ).fetchall()
        for cand_id, cand_start in sorted(
            candidates, key=lambda r: int(str(r[0]).rpartition("-")[2])
        ):
            close = (
                abs(datetime.fromisoformat(cand_start) - game.start_time)
                <= SAME_GAME_START_TOLERANCE
            )
            if close and not self._claimed_by_other_native_id(cand_id, game.provider_ids):
                return str(cand_id)
        # Genuinely new game: keep the provider's number unless another native
        # id already claimed it or it collides with a far-apart start time.
        while self._claimed_by_other_native_id(game_id, game.provider_ids) or (
            self._stored_start_time_conflicts(game_id, game.start_time)
        ):
            base, _, number = game_id.rpartition("-")
            game_id = f"{base}-{int(number) + 1}"
        return game_id

    def _claimed_by_other_native_id(self, game_id: str, provider_ids: dict[str, str]) -> bool:
        for provider, native_id in provider_ids.items():
            row = self._conn.execute(
                "SELECT native_id FROM provider_game_ids WHERE game_id = ? AND provider = ?",
                (game_id, provider),
            ).fetchone()
            if row is not None and row[0] != native_id:
                return True
        return False

    def _stored_start_time_conflicts(self, game_id: str, start_time: datetime) -> bool:
        """True if games already holds this game_id with a start_time too far from
        `start_time` to be the same physical game (rule 3 in _resolve_game_id)."""
        row = self._conn.execute(
            "SELECT start_time FROM games WHERE game_id = ?", (game_id,)
        ).fetchone()
        if row is None:
            return False
        stored = datetime.fromisoformat(row[0])
        return abs(stored - start_time) > SAME_GAME_START_TOLERANCE

    def games(
        self,
        on_date: date | None = None,
        *,
        window: tuple[datetime, datetime] | None = None,
    ) -> list[Game]:
        """Stored games, optionally narrowed.

        `on_date` matches a UTC calendar date. `window` is a half-open UTC
        [start, end) instant range — use it when the caller's notion of a day is
        not UTC's (a local-day board spans two UTC dates), since `on_date` would
        silently drop games that fall on the other side of midnight UTC.
        """
        sql = "SELECT game_id, start_time, home_team, away_team FROM games"
        params: tuple[str, ...] = ()
        if window is not None:
            sql += " WHERE start_time >= ? AND start_time < ?"
            params = (_utc_key(window[0]), _utc_key(window[1]))
        elif on_date is not None:
            sql += " WHERE substr(start_time, 1, 10) = ?"
            params = (on_date.isoformat(),)
        sql += " ORDER BY start_time"
        games = []
        for game_id, start_time, home, away in self._conn.execute(sql, params):
            provider_ids = dict(
                self._conn.execute(
                    "SELECT provider, native_id FROM provider_game_ids WHERE game_id = ?",
                    (game_id,),
                )
            )
            games.append(
                Game(
                    game_id=game_id,
                    start_time=datetime.fromisoformat(start_time),
                    home_team=home,
                    away_team=away,
                    provider_ids=provider_ids,
                )
            )
        return games

    def latest_odds(
        self,
        on_date: date | None = None,
        *,
        window: tuple[datetime, datetime] | None = None,
    ) -> list[GameOdds]:
        """Latest stored quotes per (game, provider, book, market), as domain objects.

        A book/market that drops out of newer fetch cycles keeps its last-known
        quotes (SPEC FR1: partial results are stored as-is, so per-cycle book
        coverage varies). Quotes are grouped into one GameOdds per (game,
        provider) whose fetched_at is the newest snapshot that contributed.

        `on_date` / `window` narrow the games considered, as in games(). Passing
        one matters at scale: unfiltered, the inner MAX(fetched_at) aggregate
        groups over the whole append-only odds table, so an unnarrowed call gets
        monotonically slower for the life of the database.
        """
        games = {g.game_id: g for g in self.games(on_date, window=window)}
        if not games:
            return []
        date_filter = ""
        params: tuple[str, ...] = ()
        if window is not None:
            date_filter = (
                " JOIN games AS g ON g.game_id = o2.game_id"
                " WHERE g.start_time >= ? AND g.start_time < ?"
            )
            params = (_utc_key(window[0]), _utc_key(window[1]))
        elif on_date is not None:
            date_filter = (
                " JOIN games AS g ON g.game_id = o2.game_id"
                " WHERE substr(g.start_time, 1, 10) = ?"
            )
            params = (on_date.isoformat(),)
        # Boards are game-line views: prop rows (player set) are ladders with
        # their own identity semantics and never belong on them (D-018).
        prop_filter = " AND o2.player IS NULL" if date_filter else " WHERE o2.player IS NULL"
        rows = self._conn.execute(
            f"""
            SELECT o.game_id, o.provider, o.fetched_at,
                   o.book, o.market, o.outcome, o.line, o.price
            FROM odds AS o
            JOIN (
                SELECT o2.game_id, o2.provider, o2.book, o2.market,
                       MAX(o2.fetched_at) AS fetched_at
                FROM odds AS o2{date_filter}{prop_filter}
                GROUP BY o2.game_id, o2.provider, o2.book, o2.market
            ) AS latest
              ON  latest.game_id = o.game_id
              AND latest.provider = o.provider
              AND latest.book = o.book
              AND latest.market = o.market
              AND latest.fetched_at = o.fetched_at
            ORDER BY o.game_id, o.provider, o.id
            """,
            params,
        ).fetchall()
        # Keyed per outcome so two rows tying on MAX(fetched_at) — e.g. a cycle
        # stored twice with the same timestamp — yield one quote, not two. Rows
        # arrive ordered by o.id, so the overwrite keeps the last-written row.
        quotes_by_key: dict[tuple[str, str], dict[tuple[str, str, str], Quote]] = defaultdict(
            dict
        )
        newest_by_key: dict[tuple[str, str], str] = {}
        for game_id, provider, fetched_at, book, market, outcome, line, price in rows:
            key = (game_id, provider)
            quotes_by_key[key][(book, market, outcome)] = Quote(
                book=book, market=market, outcome=outcome, line=line, price=price
            )
            # ISO-8601 UTC strings sort lexically by instant.
            if fetched_at > newest_by_key.get(key, ""):
                newest_by_key[key] = fetched_at
        return [
            GameOdds(
                game=games[game_id],
                fetched_at=datetime.fromisoformat(newest_by_key[(game_id, provider)]),
                provider=provider,
                quotes=list(quotes.values()),
            )
            for (game_id, provider), quotes in quotes_by_key.items()
        ]

    def closing_odds(
        self,
        on_date: date | None = None,
        *,
        window: tuple[datetime, datetime] | None = None,
    ) -> list[GameOdds]:
        """Closing lines: the newest quotes fetched at or before each game's
        start_time, per (game, provider, book, market, outcome).

        A game with no pre-start snapshot is absent entirely; a book that only
        appeared after first pitch contributes nothing. `on_date` / `window`
        narrow the games considered, as in games(). Both fetched_at and
        start_time are stored as UTC ISO-8601 strings, so the string comparison
        is an instant comparison.
        """
        games = {g.game_id: g for g in self.games(on_date, window=window)}
        if not games:
            return []
        date_filter = ""
        params: tuple[str, ...] = ()
        if window is not None:
            date_filter = " AND g.start_time >= ? AND g.start_time < ?"
            params = (_utc_key(window[0]), _utc_key(window[1]))
        elif on_date is not None:
            date_filter = " AND substr(g.start_time, 1, 10) = ?"
            params = (on_date.isoformat(),)
        rows = self._conn.execute(
            f"""
            SELECT o.game_id, o.provider, o.fetched_at,
                   o.book, o.market, o.outcome, o.line, o.price
            FROM odds AS o
            JOIN (
                SELECT o2.game_id, o2.provider, o2.book, o2.market, o2.outcome,
                       MAX(o2.fetched_at) AS fetched_at
                FROM odds AS o2
                JOIN games AS g ON g.game_id = o2.game_id
                WHERE o2.player IS NULL AND o2.fetched_at <= g.start_time{date_filter}
                GROUP BY o2.game_id, o2.provider, o2.book, o2.market, o2.outcome
            ) AS closing
              ON  closing.game_id = o.game_id
              AND closing.provider = o.provider
              AND closing.book = o.book
              AND closing.market = o.market
              AND closing.outcome = o.outcome
              AND closing.fetched_at = o.fetched_at
            ORDER BY o.game_id, o.provider, o.id
            """,
            params,
        ).fetchall()
        # Keyed per outcome so rows tying on MAX(fetched_at) dedup to the
        # last-written one (rows arrive ordered by o.id).
        quotes_by_key: dict[tuple[str, str], dict[tuple[str, str, str], Quote]] = defaultdict(
            dict
        )
        newest_by_key: dict[tuple[str, str], str] = {}
        for game_id, provider, fetched_at, book, market, outcome, line, price in rows:
            key = (game_id, provider)
            quotes_by_key[key][(book, market, outcome)] = Quote(
                book=book, market=market, outcome=outcome, line=line, price=price
            )
            if fetched_at > newest_by_key.get(key, ""):
                newest_by_key[key] = fetched_at
        return [
            GameOdds(
                game=games[game_id],
                fetched_at=datetime.fromisoformat(newest_by_key[(game_id, provider)]),
                provider=provider,
                quotes=list(quotes.values()),
            )
            for (game_id, provider), quotes in quotes_by_key.items()
        ]

    def store_games(self, games: list[Game]) -> int:
        """Persist schedule-only games (no odds yet): same identity
        reconciliation as store(), games and provider ids only (D-031)."""
        stored = 0
        with self._conn:
            for game in games:
                game.game_id = self._resolve_game_id(game)
                cur = self._conn.execute(
                    """
                    INSERT INTO games (game_id, start_time, home_team, away_team, season)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (game_id) DO UPDATE SET start_time = excluded.start_time
                    """,
                    (game.game_id, game.start_time.isoformat(), game.home_team,
                     game.away_team, game.season),
                )
                stored += cur.rowcount
                self._conn.executemany(
                    "INSERT OR IGNORE INTO provider_game_ids (game_id, provider, native_id)"
                    " VALUES (?, ?, ?)",
                    [(game.game_id, p, n) for p, n in game.provider_ids.items()],
                )
        return stored

    def upsert_probables(
        self, game_id: str, away_pitcher: str | None, home_pitcher: str | None,
        *, fetched_at: datetime,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO probables (game_id, away_pitcher, home_pitcher, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (game_id) DO UPDATE SET
                    away_pitcher = excluded.away_pitcher,
                    home_pitcher = excluded.home_pitcher,
                    fetched_at = excluded.fetched_at
                """,
                (game_id, away_pitcher, home_pitcher, _utc_key(fetched_at)),
            )

    def upsert_statcast_team(
        self, rows: list[tuple[str, int, int, float | None, float | None, float | None]],
        *, fetched_at: datetime,
    ) -> None:
        """(team, season, pa, xba, xslg, xwoba) rows."""
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO statcast_team (team, season, pa, xba, xslg, xwoba, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (team, season) DO UPDATE SET
                    pa = excluded.pa, xba = excluded.xba, xslg = excluded.xslg,
                    xwoba = excluded.xwoba, fetched_at = excluded.fetched_at
                """,
                [(t, s2, pa, xba, xslg, xwoba, _utc_key(fetched_at))
                 for t, s2, pa, xba, xslg, xwoba in rows],
            )

    def upsert_statcast_pitcher(
        self,
        rows: list[
            tuple[str, int, int, float | None, float | None, float | None, float | None]
        ],
        *, fetched_at: datetime,
    ) -> None:
        """(name, season, pa, xba, xslg, xwoba, xera) rows."""
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO statcast_pitcher
                    (name, season, pa, xba, xslg, xwoba, xera, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (name, season) DO UPDATE SET
                    pa = excluded.pa, xba = excluded.xba, xslg = excluded.xslg,
                    xwoba = excluded.xwoba, xera = excluded.xera,
                    fetched_at = excluded.fetched_at
                """,
                [(n, s2, pa, xba, xslg, xwoba, xera, _utc_key(fetched_at))
                 for n, s2, pa, xba, xslg, xwoba, xera in rows],
            )

    def scout(self, game_id: str) -> dict[str, object] | None:
        """Everything the matchup card needs, or None if the game is unknown."""
        game_row = self._conn.execute(
            "SELECT away_team, home_team, season FROM games WHERE game_id = ?", (game_id,)
        ).fetchone()
        if game_row is None:
            return None
        away, home, season = game_row
        prob = self._conn.execute(
            "SELECT away_pitcher, home_pitcher FROM probables WHERE game_id = ?", (game_id,)
        ).fetchone()

        def team_line(team: str) -> dict[str, object] | None:
            row = self._conn.execute(
                "SELECT pa, xba, xslg, xwoba FROM statcast_team"
                " WHERE team = ? AND season = ?",
                (team, season),
            ).fetchone()
            return dict(zip(("pa", "xba", "xslg", "xwoba"), row, strict=True)) if row else None

        def pitcher_line(name: str | None) -> dict[str, object] | None:
            if not name:
                return None
            row = self._conn.execute(
                "SELECT pa, xba, xslg, xwoba, xera FROM statcast_pitcher"
                " WHERE name = ? AND season = ?",
                (name, season),
            ).fetchone()
            return (
                dict(zip(("pa", "xba", "xslg", "xwoba", "xera"), row, strict=True))
                if row
                else None
            )

        return {
            "away_team": away,
            "home_team": home,
            "away_batting": team_line(away),
            "home_batting": team_line(home),
            "away_pitcher": prob[0] if prob else None,
            "home_pitcher": prob[1] if prob else None,
            "away_pitcher_line": pitcher_line(prob[0] if prob else None),
            "home_pitcher_line": pitcher_line(prob[1] if prob else None),
        }

    def record_result(
        self, game_id: str, home_score: int, away_score: int, *, fetched_at: datetime
    ) -> None:
        """Store a final score. Presence of a row means the game is final;
        re-recording overwrites (stat corrections happen)."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO results (game_id, home_score, away_score, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (game_id) DO UPDATE
                    SET home_score = excluded.home_score,
                        away_score = excluded.away_score,
                        fetched_at = excluded.fetched_at
                """,
                (game_id, home_score, away_score, _utc_key(fetched_at)),
            )

    def result(self, game_id: str) -> tuple[int, int] | None:
        """(home_score, away_score) if the game is final, else None."""
        row = self._conn.execute(
            "SELECT home_score, away_score FROM results WHERE game_id = ?", (game_id,)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def games_missing_results(self, *, before: datetime) -> list[Game]:
        """Stored games that started before `before` and have no final score —
        the collector's work list."""
        cutoff = _utc_key(before)
        games = []
        for game_id, start_time, home, away in self._conn.execute(
            """
            SELECT g.game_id, g.start_time, g.home_team, g.away_team
            FROM games AS g LEFT JOIN results AS r ON r.game_id = g.game_id
            WHERE r.game_id IS NULL AND g.start_time < ?
            ORDER BY g.start_time
            """,
            (cutoff,),
        ):
            games.append(
                Game(
                    game_id=game_id,
                    start_time=datetime.fromisoformat(start_time),
                    home_team=home,
                    away_team=away,
                )
            )
        return games

    def history_rows(
        self, game_id: str
    ) -> list[tuple[str, str, str, str, str, float | None, int, str | None]]:
        """(fetched_at, provider, book, market, outcome, line, price, player)
        ordered by time."""
        return self._conn.execute(
            """
            SELECT fetched_at, provider, book, market, outcome, line, price, player
            FROM odds WHERE game_id = ? ORDER BY fetched_at, book, market, outcome
            """,
            (game_id,),
        ).fetchall()

    def all_rows(
        self, on_date: date | None = None
    ) -> list[tuple[str, str, str, str, str, str, str, str, str, float | None, int, str | None]]:
        """Odds joined with game context, for flat exports/DataFrames."""
        sql = """
            SELECT o.game_id, g.start_time, g.away_team, g.home_team,
                   o.fetched_at, o.provider, o.book, o.market, o.outcome, o.line, o.price,
                   o.player
            FROM odds o JOIN games g ON g.game_id = o.game_id
        """
        params: tuple[str, ...] = ()
        if on_date is not None:
            sql += " WHERE substr(g.start_time, 1, 10) = ?"
            params = (on_date.isoformat(),)
        sql += " ORDER BY o.fetched_at, o.game_id, o.book, o.market, o.outcome"
        return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        self._conn.close()
