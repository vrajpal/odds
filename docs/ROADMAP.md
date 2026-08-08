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
- [x] All SPEC acceptance criteria checked off (7/7 — the live `collect --once`
      criterion verified 2026-07-29/30 against the real API; v1 complete)
- **Demo:** cron-style `mlb-odds collect --once`; `mlb-odds today` renders the board.

## M4 — Post-v1 (unordered backlog)
- [x] ESPN consensus-line provider (free, no key — validates pluggability for
      real) — shipped as `providers.ESPN`; note it now carries a partner book's
      lines, not a consensus (D-016)
- [x] `changed_only` write mode (dedup consecutive identical quotes)
- [x] Player props market support — curated markets via `mlb-odds props` /
      `client.fetch_and_store_props()`; ladders keyed per (player, line) (D-018)
- [x] Closing-line convenience queries (last snapshot before start_time)
- [x] Live/in-game polling mode — `collect --live` polls only inside per-game
      live windows computed from the stored slate (D-017)
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

## M5 — Multi-sport
- [x] NFL game lines: sport-parameterized providers (The Odds API + ESPN),
      32-team registry, `spread` market, per-sport databases, `--sport` on every
      CLI command (D-019)
- [x] NFL player props: four curated O/U ladders, sport-gated markets,
      edited fixture pending in-season re-recording (D-022)
- [x] Web API/UI sport switcher: `?sport=` on every data endpoint, per-sport
      databases, MLB/NFL toggle in the React UI (D-022)

## C — Circa Million consensus tool (see circa-million-2026-rules.md)
- [x] C1 — Board: contest calendar (Rule 19 weeks, Sat 4 PM PT deadline,
      holiday line posts), ContestStore for manual Circa lines (own DB, D-020),
      edge/consensus/movement math over the NFL odds DB, contest_api board +
      line entry endpoints with countdown
- [x] C2 — Consensus: blind one-shot proposals, reveal, stance voting,
      captain rotation, card lock with Rule-8 enforcement, ETSN tracking (D-021)
- [x] C3 — Season: grading entry, 1st-place tiebreaker ladder, quarter
      standings, booby-guard alerts, static contest UI at / (D-021)

## C4 — Stats for picking (see circa-million-2026-rules.md)
- [x] C4.1 — Results collector: `results` table + `mlb-odds results` from the
      ESPN scoreboard, Sunday-closing-poll cron documented (D-024)
- [x] C4.2 — CLV report: per-pick + season aggregates on the Season tab (D-025)
- [x] C4.3 — Edge calibration: cover rate by at-lock edge bucket + key numbers (D-025)
- [x] C4.4 — Market-implied power ratings (ridge LSQ) + model line on the board (D-025)
- [x] C4.5 — Situational flags (rest, rest differential, divisional) + member
      proposal/stance/captain records (D-025)
