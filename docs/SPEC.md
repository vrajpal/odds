# mlb-odds — Specification

A Python library that fetches, normalizes, and stores betting odds for Major League
Baseball games, with a pluggable provider layer so data sources can be added or swapped.

## Goals

1. Fetch current MLB game lines (moneyline, run line, totals) from one or more providers.
2. Normalize everything into one canonical schema — one set of team codes, one game
   identity, one odds representation — regardless of source.
3. Persist timestamped snapshots to SQLite so line movement and closing lines can be
   analyzed after the fact.
4. Expose the data three ways: Python objects / pandas DataFrames, a CLI, and a
   scheduled collector for unattended polling.

## Non-goals (v1)

- Player props, futures, live/in-game odds (roadmap, not v1).
- Sports other than MLB (but nothing in the schema should hard-block adding them).
- Bet placement, bankroll tracking, or modeling — this library stops at clean data.
- A web UI or API server.
- Multi-machine or multi-writer deployment (SQLite single-writer is an accepted
  constraint; see docs/DECISIONS.md).

## Functional requirements

### FR1 — Providers
- A provider is any class implementing the `OddsProvider` protocol
  (see docs/ARCHITECTURE.md). Adding a provider must require **no changes** to
  storage, client, collector, or CLI code.
- v1 ships with one provider: **The Odds API** (the-odds-api.com, v4).
  - Sport key `baseball_mlb`, markets `h2h,spreads,totals`, region `us`,
    odds format `american`.
  - API key via `THE_ODDS_API_KEY` env var or explicit constructor arg.
  - Must read the `x-requests-remaining` response header and surface it
    (log line + accessible on the provider) so quota is never a surprise.
- Provider failures must not crash the collector: log the error, skip the cycle,
  continue. Partial results (some books missing) are stored as-is.

### FR2 — Normalization
- Teams: canonical MLB abbreviations (e.g. `NYY`, `LAD`, `ATH`), defined once in
  `teams.py` with per-provider name mappings. An unrecognized team name is a hard
  error in tests and a logged skip in production.
- Times: UTC everywhere in models and storage. Local time is a display concern (CLI).
- Prices: American odds (int) are canonical; decimal odds are derived, not stored.
- Game identity: a deterministic canonical `game_id` built from
  `(date_utc, away_team, home_team, game_number)` — `game_number` disambiguates
  doubleheaders. Provider-native IDs are stored alongside for traceability.

### FR3 — Storage
- SQLite, WAL mode, single writer. Schema in docs/ARCHITECTURE.md.
- Every fetch appends a full snapshot (no dedup in v1 — volume math says ~5–10 GB/yr
  worst case, acceptable). A `changed_only` write mode is a roadmap item.
- DB path: `--db` flag / `MLB_ODDS_DB` env var / default `./odds.sqlite`.

### FR4 — Library API
```python
from mlb_odds import OddsClient
from mlb_odds.providers import TheOddsAPI

client = OddsClient(providers=[TheOddsAPI()], db="odds.sqlite")
snapshot = client.fetch_and_store()      # poll all providers, persist, return models
games    = client.current_odds()          # latest stored odds as domain objects
df       = client.history_df(game_id)     # line movement as a DataFrame
df2      = client.odds_df(on_date=...)    # flat DataFrame of stored odds
```

### FR5 — CLI (`mlb-odds`)
- `mlb-odds collect --once` — one fetch cycle, then exit (cron-friendly).
- `mlb-odds collect --interval 300` — poll loop until interrupted.
- `mlb-odds today` — table of today's games with latest moneyline / run line / total.
- `mlb-odds history <game>` — line movement for one game (accepts canonical game_id
  or a fuzzy `AWAY@HOME` on a date).
- `mlb-odds export --format csv|parquet --out PATH` — dump stored odds.

### FR6 — Quota budgeting
The Odds API free tier is 500 credits/month and a game-lines request costs
`markets × regions` = 3 credits → ~5 polls/day. The collector must log credits
remaining each cycle, and the docs must state the interval → monthly-cost math so
polling frequency is a conscious choice.

## Acceptance criteria (v1 is done when all of these pass)

- [ ] `pip install -e .` then `mlb-odds collect --once` (with a key set) populates a
      fresh SQLite file with today's games and per-book odds rows.
      (Code path fully tested offline; final check pending a live API key.)
- [x] `mlb-odds today` renders a readable table from stored data with no network calls.
- [x] `client.history_df(game_id)` returns a DataFrame with one row per
      (fetched_at, book, market, outcome) suitable for plotting line movement.
- [x] Full test suite passes **offline** — provider tests run against recorded JSON
      fixtures in `tests/fixtures/`, storage tests against a temp SQLite file.
- [x] A second provider can be added by writing one module implementing the protocol
      and registering it — demonstrated by a `FakeProvider` used in tests.
- [x] Doubleheaders produce two distinct game_ids; team-name normalization covers all
      30 clubs for The Odds API's naming.
- [x] Collector survives a provider outage (simulated in tests) and keeps polling.

## Constraints & conventions

- Python ≥ 3.11, `src/` layout, packaged with `pyproject.toml` (hatchling), managed
  with `uv`.
- Runtime deps kept minimal: `httpx`, `pydantic` (v2), `typer`, `pandas`.
- Lint/format: `ruff`. Types: `mypy` on `src/`. Tests: `pytest`.
- No live network calls in tests or CI, ever.
