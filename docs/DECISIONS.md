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
