# Roadmap

Milestones are ordered so every one ends with something runnable. Check items off as
they land; v1 = M1–M3 complete and all SPEC acceptance criteria green.

## M1 — Core models + provider (fetch works)
- [x] Project scaffolding: pyproject.toml, src layout, uv, ruff, mypy, pytest wiring
- [x] `models.py` — Game, Quote, GameOdds
- [x] `teams.py` — 30 canonical codes + The Odds API name mapping, with tests
- [x] `providers/base.py` — OddsProvider protocol, ProviderError
- [x] `providers/the_odds_api.py` + recorded fixtures (normal day, doubleheader,
      unknown team, missing market, 500)
- [x] Quota header surfaced and logged
- **Demo:** a scratch script prints today's normalized odds from the live API.
  (`scripts/demo_fetch.py` written; live run pending an API key.)

## M2 — Storage + client (persistence works)
- [x] `storage.py` — schema, WAL, migration runner, upsert/append, queries
- [x] `client.py` — fetch_and_store, current_odds, history_df, odds_df
- [x] FakeProvider in conftest; pluggability + outage-survival tests
- **Demo:** two fetch cycles minutes apart → `history_df` shows both timestamps.

## M3 — Collector + CLI (v1 complete)
- [x] `collector.py` — loop, --once mode, clean SIGINT, cycle summary logging
- [x] `cli.py` — collect / today / history / export
- [x] README: install, API key setup, quota math (interval → credits/month), examples
- [ ] All SPEC acceptance criteria checked off (6/7 — the live `collect --once`
      criterion is pending an API key; everything around it is tested offline)
- **Demo:** cron-style `mlb-odds collect --once`; `mlb-odds today` renders the board.

## M4 — Post-v1 (unordered backlog)
- [ ] ESPN consensus-line provider (free, no key — validates pluggability for real)
- [x] `changed_only` write mode (dedup consecutive identical quotes)
- [ ] Player props market support
- [x] Closing-line convenience queries (last snapshot before start_time)
- [ ] Live/in-game polling mode
- [x] Parquet export of full history for DuckDB workflows — already shipped as
      `mlb-odds export --format parquet` (odds_df carries every snapshot row
      joined with game context); verified + README DuckDB example added

### Test-rigor backlog (adversarial-review findings, minor, not blocking v1)
- [x] FR6 quota logging: caplog assertion that the collector's cycle summary
      reaches the log (mutation deleting the log line currently passes)
- [x] Local-timezone display: TZ-monkeypatched fixture (e.g. America/New_York +
      time.tzset) asserting rendered times in `today`/`history`, with a start time
      crossing the UTC date boundary to pin the local-date filter
- [x] SIGINT handler: assert signal.getsignal(SIGINT) is swapped during
      collector.run and restored after
- [x] CLI collect loop-mode wiring: a test that fails if `once=True` is hardcoded
- [x] Migration runner upgrade path: apply migration N+1 to an existing v-N DB
- [x] latest_odds: dedup ties when two rows share an identical fetched_at
- [x] latest_odds(on_date)/current_odds(on_date) date-filter branch coverage
- [x] Cross-provider doubleheader convergence when providers report in reverse order
