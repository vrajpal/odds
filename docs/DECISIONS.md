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
