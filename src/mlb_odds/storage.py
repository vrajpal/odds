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

    def store(self, results: list[GameOdds]) -> int:
        """Persist one fetch cycle: upsert games, append all quotes. Returns rows written.

        Game identity is reconciled against previously stored native ids first
        (see _resolve_game_id), and the models in `results` are updated in place
        to carry the canonical game_id.
        """
        written = 0
        with self._conn:
            for game_odds in results:
                game = game_odds.game
                game.game_id = self._resolve_game_id(game)
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
                                      line, price)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                        )
                        for q in game_odds.quotes
                    ],
                )
                written += len(game_odds.quotes)
        return written

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
        rows = self._conn.execute(
            f"""
            SELECT o.game_id, o.provider, o.fetched_at,
                   o.book, o.market, o.outcome, o.line, o.price
            FROM odds AS o
            JOIN (
                SELECT o2.game_id, o2.provider, o2.book, o2.market,
                       MAX(o2.fetched_at) AS fetched_at
                FROM odds AS o2{date_filter}
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

    def history_rows(self, game_id: str) -> list[tuple[str, str, str, str, str, float | None, int]]:
        """(fetched_at, provider, book, market, outcome, line, price) ordered by time."""
        return self._conn.execute(
            """
            SELECT fetched_at, provider, book, market, outcome, line, price
            FROM odds WHERE game_id = ? ORDER BY fetched_at, book, market, outcome
            """,
            (game_id,),
        ).fetchall()

    def all_rows(
        self, on_date: date | None = None
    ) -> list[tuple[str, str, str, str, str, str, str, str, str, float | None, int]]:
        """Odds joined with game context, for flat exports/DataFrames."""
        sql = """
            SELECT o.game_id, g.start_time, g.away_team, g.home_team,
                   o.fetched_at, o.provider, o.book, o.market, o.outcome, o.line, o.price
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
