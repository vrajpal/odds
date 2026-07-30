# Decision log

Format: one entry per decision, newest at the bottom. Revisit by appending a new
entry, never by rewriting history.

## D-001 — Python (2026-07-09)
Standard stack for sports-data/quant work; pandas interop was an explicit output
requirement. TypeScript and Go considered and rejected (no web-app consumer, and
ad-hoc analysis matters more than daemon ergonomics).

## D-002 — Pluggable provider architecture (2026-07-09)
Odds sources are volatile: APIs change pricing, scrapers break, books come and go.
A one-method protocol (`OddsProvider.fetch_game_lines`) isolates that churn. Cost is
one indirection layer; accepted.

## D-003 — The Odds API as the v1 provider (2026-07-09)
Chosen over scraping DraftKings/FanDuel (brittle, ToS-gray, maintenance-heavy) and
aggregator scraping (same issues). Free tier: 500 credits/month; a game-lines call
costs 3 credits (3 markets × 1 region) → ~5 polls/day free. That bounds v1 polling
frequency; per-book depth across ~15 US books is the payoff. ESPN's free consensus
endpoint is the planned no-key fallback provider (roadmap M4).

## D-004 — v1 scope: game lines only (2026-07-09)
Moneyline, run line, totals. Props multiply volume 10–20× and cost more credits;
futures and live odds need different polling models. All deferred, none precluded by
the schema (market is a TEXT column, not an enum in SQL).

## D-005 — SQLite storage (2026-07-09)
Volume math: ~2,430 games/season × ~90 rows/snapshot × ~290 snapshots/game ≈ 60–65M
rows ≈ 5–10 GB/season at aggressive (5-min) polling — comfortably within SQLite
range for years. The real limits are topological, and we accept them knowingly:
- exactly one writer process (WAL mode: many readers + one writer) — fine, there is
  one collector;
- local-file access only — collector and analysis run on the same machine;
- never on NFS.
Migration trigger: a second writer or a second machine → Postgres. Hedge: portable
SQL schema, storage behind its own module. Analytical speed is not a migration
trigger — DuckDB can query the SQLite file directly.

## D-006 — Append-only snapshots, no dedup in v1 (2026-07-09)
Every fetch cycle writes all quotes even if unchanged. Simpler writes, simpler
"what did the board look like at time T" queries. Space is cheap per D-005.
`changed_only` mode is roadmap if the file ever gets annoying.

## D-007 — American odds as canonical price (2026-07-09)
US books quote American; The Odds API returns it natively as integers (no float
round-trip). Decimal is derived on the model (`price_decimal` property), never stored.

## D-008 — Canonical game identity (2026-07-09)
`game_id = "{date_utc}-{away}-{home}-{game_number}"`. Deterministic across providers
(no provider's native ID is privileged), human-readable, doubleheader-safe.
Provider-native IDs kept in a side table for traceability/debugging.

## D-009 — Game identity is reconciled in storage, not per-fetch (2026-07-09)
The Odds API drops finished games from the /odds feed, so a provider can only
number doubleheaders from what the current response contains — game 2 alone in
the feed looks like game 1. `Storage.store` therefore treats provider-assigned
game_numbers as provisional: a native id seen before keeps its stored game_id,
and a new game never takes a game_id already claimed by a different native id
from the same provider (its number is bumped). Consequence: game_number is
stable-first-assignment, not strictly start-time-ordered, when the halves of a
doubleheader first appear in different cycles. Stability wins (FR2 / D-008).

## D-010 — Cross-provider convergence requires start-time agreement (2026-07-09)
D-009's native-id claim check is per-provider by design, so a second provider's
first sighting of a doubleheader converges onto whatever game_id its provisional
number computes — which is wrong when its feed has already dropped game 1 and it
numbers game 2 as 1, misfiling game 2's quotes under game 1's id (and rule 1 then
freezes the bad mapping). Guard: a new native id may only take an *existing*
game_id if the stored start_time is within 2 hours of the incoming one; otherwise
the number is bumped, same as a same-provider claim conflict. 2 hours is below any
doubleheader gap (game 1 alone runs ~2.5h+) but above realistic provider disagreement
about one game's first pitch. Accepted tradeoff: a game rescheduled by >2h within
the same day, first seen by a new provider only after the move while another
provider stored the old time, splits into two game_ids — rarer and cheaper than
cross-wiring two physical games' line histories.

## D-011 — .env support at the CLI layer only (2026-07-11)
The CLI loads a `.env` file from the working directory via `python-dotenv`
(`load_dotenv()` in the Typer app callback), so cron jobs and local runs can keep
`THE_ODDS_API_KEY` / `MLB_ODDS_DB` in one gitignored, chmod-600 file instead of a
shell profile or crontab line. The library never reads `.env`: `import mlb_odds`
must not have cwd-dependent side effects when embedded in a larger app — library
users pass `TheOddsAPI(api_key=...)` or set real env vars. Real environment
variables always win (`load_dotenv` does not override existing vars). New runtime
dep `python-dotenv`: zero transitive dependencies, clears the minimal-deps bar.

## D-012 — The API resolves the database server-side, read-only (2026-07-25)
The HTTP API's first revision copied `_resolve_db` from the CLI, which accepts a
`--db` override. In the CLI that flag is trusted local-operator configuration; as
an HTTP query parameter it became unauthenticated attacker input flowing into
`sqlite3.connect` on a connection that opens read-write and runs `_migrate()`.
Confirmed consequences: arbitrary-path SQLite file creation, reading any other
mlb-odds database the process could reach, and injecting this schema (plus a
permanent `journal_mode=WAL` flip) into any unrelated SQLite database on the host
— a browser cookie store, for instance. Two changes, defence in depth:

1. No endpoint takes a `db` parameter. The API path comes from `MLB_ODDS_DB` or
   the default, resolved server-side. Database location is deployment config.
2. `Storage(..., read_only=True)` opens via the `mode=ro` URI and skips both the
   WAL pragma and `_migrate()`, so an API process cannot create or write a
   database even if a future change reintroduces a caller-supplied path. The path
   is passed through `Path.resolve().as_uri()` so `?`/`#` cannot smuggle extra URI
   parameters. A missing database is a 503, not a silently created empty one.

`OddsClient` additionally rejects `read_only=True` with a non-empty `providers`
list. The API's inability to burn The Odds API credits was previously incidental
(it happened to pass `providers=[]`); this makes it a checked invariant, since a
"refresh now" endpoint is an obvious future addition and 167 unauthenticated
requests would exhaust a month of free-tier quota (3 credits/poll, 500/month).

Consequence: the API cannot apply migrations. A database created by an older
build is migrated on the next CLI or collector run; until then the API reads it
at the old schema. Given the collector must run anyway for the data to exist,
this is not a practical ordering hazard.

## D-013 — Local-day queries use a UTC instant window, not a UTC date (2026-07-25)
`games(on_date)` / `latest_odds(on_date)` match `substr(start_time, 1, 10)`, i.e.
a UTC calendar date. The `today` board is a *local*-day view, so filtering by UTC
date is the wrong question: a 10pm PDT first pitch is 05:00Z the following day and
vanishes from its own board. The CLI sidestepped this by fetching everything and
filtering in Python — correct, but it made the query cost grow with the whole
append-only `odds` table (measured: 1.07s to render ~15 games from a 216k-row
database, and rising for the life of the database) because the inner
`MAX(fetched_at)` aggregate groups over every row ever stored.

Both methods therefore take an optional `window=(start, end)` of timezone-aware
UTC instants, half-open, which the API computes for the viewer's local day. Bounds
are normalized to UTC before comparison, preserving the invariant that makes
lexical ISO-8601 comparison equal instant comparison (see `models._require_utc`).
`on_date` is retained and unchanged for callers that genuinely mean a UTC date.

Migration 2 adds `CREATE INDEX idx_games_start ON games (start_time)` so the range
scan is an index seek rather than a table scan — without it the window helps the
empty-day case only. Measured on the same 216k-row database: 1.07s → 0.006s (185x)
for a day with games, confirmed via `EXPLAIN QUERY PLAN` to use the new index.

## D-014 — Doubleheader convergence must search, not just bump (2026-07-29)
The test-rigor backlog's "cross-provider doubleheader, reverse order" item
exposed a real defect, not a coverage gap: `_resolve_game_id` started from the
provider's own game number and only ever bumped upward. A provider that saw the
doubleheader in the opposite order numbers the halves 2/1; its early half
(numbered 2) could never reach canonical id 1 and split off as a phantom game 3.
Resolution now first tries to converge onto any stored same-slate game whose
start_time is within SAME_GAME_START_TOLERANCE and whose id isn't claimed by a
different native id of the same provider; only then does it fall back to the
bump-until-free allocation for genuinely new games. D-008/D-010 semantics are
unchanged — this widens the candidate set, not the matching rules.

## D-015 — changed_only dedups against the newest row only (2026-07-30)
`--changed-only` skips a quote when its (line, price) equals the newest stored
row for the same (game, provider, book, market, outcome). The baseline is
deliberately the newest row, not "any prior value": a reversion (120 → 125 →
120) must write all three rows or history would show a phantom 125 as current.
The dedup lives in Storage.store, not the collector, so library users get the
same semantics. Trade-off documented in the README: with changed_only, history
records changes rather than polls — the absence of a row at time T means
"unchanged since the previous row", not "book dropped out". latest_odds is
unaffected either way, because it already carries last-known quotes forward.
Default stays append-everything; storage cost was never the motivation for the
default, auditability of "we polled at T and saw X" was.
