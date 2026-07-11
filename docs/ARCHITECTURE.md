# mlb-odds — Architecture

## Package layout

```
src/mlb_odds/
  __init__.py          # public surface: OddsClient + models re-exported
  models.py            # pydantic v2 domain models (Game, Quote, GameOdds)
  teams.py             # canonical team codes + per-provider name mappings
  providers/
    __init__.py
    base.py            # OddsProvider protocol + ProviderError
    the_odds_api.py    # v1 concrete provider
  storage.py           # SQLite schema, migrations, writes, queries
  client.py            # OddsClient — orchestrates providers + storage
  collector.py         # polling loop (used by CLI `collect`)
  cli.py               # typer app, entrypoint `mlb-odds`
tests/
  fixtures/            # recorded provider JSON responses
  conftest.py          # FakeProvider, temp-db fixture
```

Dependency direction is strictly one-way:
`cli → collector → client → (providers, storage) → models/teams`.
Providers never import storage; storage never imports providers. Both speak only in
domain models.

## Domain models (`models.py`)

```python
Market  = Literal["moneyline", "run_line", "total"]
Outcome = Literal["home", "away", "over", "under"]

class Game(BaseModel):
    game_id: str            # canonical: "2026-07-09-NYM-NYY-1" (date-away-home-gamenum)
    start_time: datetime    # UTC, tz-aware
    home_team: str          # canonical code, e.g. "NYY"
    away_team: str
    provider_ids: dict[str, str] = {}   # provider name -> native id

class Quote(BaseModel):
    book: str               # e.g. "draftkings" (provider's book key, lowercased)
    market: Market
    outcome: Outcome
    line: float | None      # run_line: ±1.5 etc; total: 8.5 etc; moneyline: None
    price: int              # American odds, e.g. -145, +120

    @property
    def price_decimal(self) -> float: ...   # derived, never stored

class GameOdds(BaseModel):
    game: Game
    fetched_at: datetime    # UTC, one timestamp per fetch cycle
    provider: str
    quotes: list[Quote]
```

## Provider protocol (`providers/base.py`)

```python
class OddsProvider(Protocol):
    name: str                                   # stable key, stored in DB rows

    def fetch_game_lines(self) -> list[GameOdds]:
        """Fetch current MLB game lines for all upcoming/live games.

        Must return fully normalized models: canonical team codes, UTC times,
        American prices. Raises ProviderError on unrecoverable failure.
        """
```

Rules for every provider implementation:
- All source-specific mess (auth, pagination, naming, rate limits) stays inside the
  provider module.
- Team names go through `teams.normalize(provider_name, raw_name)` — unknown names
  raise in strict mode (tests), log-and-skip otherwise.
- One `fetched_at` timestamp per fetch call, stamped by the provider.

### The Odds API specifics (`the_odds_api.py`)
- `GET https://api.the-odds-api.com/v4/sports/baseball_mlb/odds`
  with `regions=us&markets=h2h,spreads,totals&oddsFormat=american`.
- Market mapping: `h2h → moneyline`, `spreads → run_line`, `totals → total`.
- Read `x-requests-remaining` / `x-requests-used` headers; expose as
  `provider.quota_remaining` and log each cycle.
- httpx with a sane timeout (10s) and one retry on 5xx/timeouts.

## Storage (`storage.py`)

SQLite via stdlib `sqlite3`, WAL mode on connect. Schema:

```sql
CREATE TABLE games (
    game_id     TEXT PRIMARY KEY,
    start_time  TEXT NOT NULL,        -- ISO-8601 UTC
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
    fetched_at  TEXT NOT NULL,        -- ISO-8601 UTC
    provider    TEXT NOT NULL,
    book        TEXT NOT NULL,
    market      TEXT NOT NULL,        -- moneyline | run_line | total
    outcome     TEXT NOT NULL,        -- home | away | over | under
    line        REAL,                 -- NULL for moneyline
    price       INTEGER NOT NULL      -- American odds
);

CREATE INDEX idx_odds_game    ON odds (game_id, market, fetched_at);
CREATE INDEX idx_odds_fetched ON odds (fetched_at);

CREATE TABLE schema_version (version INTEGER NOT NULL);
```

- Writes: `upsert_games()` + `append_odds()` inside one transaction per fetch cycle.
- Writes reconcile game identity against stored native ids first: a native id
  seen before keeps its game_id, and a new game never takes a game_id claimed by
  a different native id from the same provider — provider-assigned doubleheader
  numbers are provisional (see docs/DECISIONS.md D-009).
- `schema_version` + a tiny linear migration runner from day one, so schema changes
  never require hand-editing user DBs.
- Keep the schema portable SQL — a future SQLite → Postgres move should be a
  connection change plus minor dialect tweaks (see docs/DECISIONS.md D-005).

## Client (`client.py`)

`OddsClient(providers, db)` is the one object users touch:
- `fetch_and_store()` — for each provider: fetch, persist, collect errors; returns
  `list[GameOdds]`. A provider raising doesn't abort the others.
- `current_odds(on_date=None)` — latest snapshot per (game, book, market) from storage.
- `history_df(game_id)` / `odds_df(...)` — pandas views over storage queries.

## Collector (`collector.py`)

A plain loop: `fetch_and_store()`, log summary (games seen, rows written, quota
remaining), sleep `interval`, repeat. `--once` mode does a single cycle for cron.
SIGINT exits cleanly mid-sleep. No scheduler dependency — cron/systemd owns real
scheduling.

## CLI (`cli.py`)

Typer app; commands per SPEC FR5. Reads `THE_ODDS_API_KEY` and `MLB_ODDS_DB` from the
environment, falling back to a `.env` in the working directory (CLI layer only —
see docs/DECISIONS.md D-011); all display-time timestamps converted to local tz.

## Error handling & logging

- `ProviderError` for provider-level failures; everything else propagates.
- stdlib `logging`, logger name `mlb_odds`; the CLI configures a human-readable
  handler, the library never configures logging itself.

## Testing strategy

- Provider tests: recorded real JSON from The Odds API in `tests/fixtures/`, served
  via httpx `MockTransport`. Cover: normal day, doubleheader, unknown team name,
  missing market from a book, 500 response.
- Storage tests: temp SQLite file; round-trip models → rows → models, migration runner.
- Client/collector tests: `FakeProvider` (in `conftest.py`) proving the pluggability
  acceptance criterion and outage survival.
- Zero live network in tests.
