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
- [ ] `storage.py` — schema, WAL, migration runner, upsert/append, queries
- [ ] `client.py` — fetch_and_store, current_odds, history_df, odds_df
- [ ] FakeProvider in conftest; pluggability + outage-survival tests
- **Demo:** two fetch cycles minutes apart → `history_df` shows both timestamps.

## M3 — Collector + CLI (v1 complete)
- [ ] `collector.py` — loop, --once mode, clean SIGINT, cycle summary logging
- [ ] `cli.py` — collect / today / history / export
- [ ] README: install, API key setup, quota math (interval → credits/month), examples
- [ ] All SPEC acceptance criteria checked off
- **Demo:** cron-style `mlb-odds collect --once`; `mlb-odds today` renders the board.

## M4 — Post-v1 (unordered backlog)
- [ ] ESPN consensus-line provider (free, no key — validates pluggability for real)
- [ ] `changed_only` write mode (dedup consecutive identical quotes)
- [ ] Player props market support
- [ ] Closing-line convenience queries (last snapshot before start_time)
- [ ] Live/in-game polling mode
- [ ] Parquet export of full history for DuckDB workflows
