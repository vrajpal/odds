# mlb-odds

Fetch, normalize, and store MLB betting odds (moneyline, run line, totals) from
pluggable providers, with SQLite snapshot history, a CLI, and pandas access.

Every fetch appends a timestamped snapshot per book and market, so line movement
and closing lines can be analyzed after the fact. All data is normalized to one
canonical schema — canonical team codes (`NYY`, `LAD`, ...), UTC timestamps,
American odds — no matter which provider it came from.

## Install

```bash
# from a clone
uv sync                 # dev environment
# or into any environment
pip install -e .
```

Requires Python ≥ 3.11. Runtime deps: httpx, pydantic v2, typer, pandas.
Parquet export additionally needs `pip install pyarrow`.

## API key setup

v1 ships with one provider, [The Odds API](https://the-odds-api.com) (v4).
Sign up for a key and export it:

```bash
export THE_ODDS_API_KEY=your-key-here
```

Optionally set the database location (defaults to `./odds.sqlite`):

```bash
export MLB_ODDS_DB=~/data/odds.sqlite
```

Alternatively, put both in a `.env` file in the directory you run the CLI from
(keep it out of version control and `chmod 600` it):

```bash
# .env
THE_ODDS_API_KEY=your-key-here
MLB_ODDS_DB=/home/me/data/odds.sqlite
```

The CLI loads it automatically; real environment variables take precedence. Only
the CLI reads `.env` — importing `mlb_odds` as a library never does.

## Quota math — pick your interval consciously

One game-lines poll costs `markets × regions` = **3 credits**; the free tier is
**500 credits/month**.

| `--interval`          | polls/day | credits/month | fits free tier?         |
| --------------------- | --------- | ------------- | ----------------------- |
| 300 s (5 min)         | 288       | ~25,900       | no (paid tiers only)    |
| 3600 s (1 h)          | 24        | 2,160         | no                      |
| 21600 s (6 h)         | 4         | 360           | yes                     |
| cron, 5×/day, `--once` | 5        | 450           | yes (the free-tier max) |

Formula: `credits/month = (86400 / interval_seconds) × 3 × 30`. The collector
logs credits remaining every cycle so quota is never a surprise.

**Player props are metered differently**: the events list is free, but each
event's odds request costs `[markets returned] × [regions]`. A full 15-game
slate at two markets can spend up to ~30 credits per `mlb-odds props` run —
that's why there is no loop mode; cron it deliberately. Prop rows never appear
on the `today`/`closing` boards (they're ladders, not board cells — D-018);
query them via `history`, `export`, or `odds_df()`, all of which carry a
`player` column.

## CLI

```bash
mlb-odds collect --once            # one fetch cycle, then exit (cron-friendly)
mlb-odds collect --interval 21600  # poll loop until Ctrl-C (clean SIGINT exit)
mlb-odds collect --once --changed-only  # append only quotes that moved (D-015)
mlb-odds collect --live --interval 300  # poll only while games are live (D-017)
mlb-odds collect --once --provider espn # free source, no key needed (--provider all for both)

mlb-odds props --market pitcher_strikeouts --market batter_home_runs
                                   # player-prop ladders, one snapshot per run (D-018)

mlb-odds today                     # today's board from stored data — no network
mlb-odds closing --date 2026-07-09 # closing lines: last snapshot at/before first pitch
mlb-odds history 2026-07-09-NYM-NYY-1        # line movement for one game
mlb-odds history NYM@NYY --date 2026-07-09   # same, fuzzy AWAY@HOME form
mlb-odds export --format csv --out odds.csv
mlb-odds export --format parquet --out odds.parquet   # needs pyarrow
```

Exports carry the **full snapshot history** (every fetch, joined with game
context), not just the latest board — ready for DuckDB:

```sql
-- closing-line drift by book, straight off the parquet file
SELECT book, market, outcome, arg_max(price, fetched_at) AS last_price
FROM 'odds.parquet'
WHERE game_id = '2026-07-09-NYM-NYY-1' AND fetched_at <= start_time
GROUP BY book, market, outcome;
```

All commands take `--db PATH` (or `MLB_ODDS_DB`). `today` and `history` display
times in your local timezone; storage is always UTC.

With `--changed-only`, history records *changes*, not *polls*: a missing
timestamp means "unchanged since the previous row", not "book dropped out".
`today` and the API board are unaffected — they already carry last-known
quotes forward. The default remains append-everything, which additionally
records "we polled at T and the board looked like X".

Example `today` output:

```
NYM @ NYY  2026-07-09 07:05 PM EDT  [2026-07-09-NYM-NYY-1]
  book              moneyline     run line        total
  draftkings        +120/-140     -1.5 (-105)     8.5 (o-108)
  fanduel           +118/-138     -1.5 (-102)     8.5 (o-110)
```

## NFL

Everything above works for NFL game lines too — same commands, `--sport nfl`:

```bash
mlb-odds collect --once --sport nfl                  # -> ./nfl-odds.sqlite ($NFL_ODDS_DB)
mlb-odds collect --once --sport nfl --provider espn  # free source, no key
mlb-odds today --sport nfl                           # board shows spread instead of run line
mlb-odds closing --sport nfl --date 2026-09-10
mlb-odds export --sport nfl --format parquet --out nfl.parquet
```

Each sport lives in its **own database** (MLB `KC` and NFL `KC` are different
franchises that would collide on canonical game ids — D-019). The Odds API
quota is shared across sports: an NFL poll costs the same 3 credits. In the
library, pass `sport=` to a provider: `TheOddsAPI(sport="nfl")`,
`ESPN(sport="nfl")`. Not yet covered for NFL: player props, and the web UI
(which serves the MLB database).

Cron example (5 polls/day, free-tier friendly) — cron runs with a bare
environment, so keep the key in a `.env` next to the project and `cd` there first:

```cron
0 10,13,16,19,22 * * *  cd /home/me/odds && /home/me/.local/bin/uv run mlb-odds collect --once >> collect.log 2>&1
```

## Web API & UI

Run the FastAPI server:

```bash
python run_api.py
```

API runs on `http://localhost:8000` with endpoints:
- `GET /api/today` — today's games with latest odds per book (JSON)
- `GET /api/games/{game_id}/history` — line movement history
- `GET /api/export?fmt=csv|json` — export all stored odds
- `GET /api/health` — health check (`/` serves the built frontend, not JSON)

The API reads an existing database and never creates, migrates, or writes one —
run `mlb-odds collect --once` first or it returns 503. Its path comes from
`MLB_ODDS_DB` only; there is deliberately no per-request `db` override (D-012).
No endpoint can reach a provider, so HTTP traffic cannot spend API credits.

**Not hardened for untrusted networks.** There is no authentication and no rate
limiting, `/api/export` serializes the whole table into one response, and
`run_api.py` uses `--reload` (a development flag). Keep it on `127.0.0.1`, or put
authentication and a rate limiter in front of it before exposing it.

To start the React frontend:

```bash
cd frontend
npm install
npm run dev
```

Frontend runs on `http://localhost:5173` and proxies API calls. After development, build and serve from the API server:

```bash
npm run build                    # outputs to frontend/dist/
python run_api.py               # serves frontend from /
```

Open `http://localhost:8000` in your browser. The API serves `frontend/dist/`
at `/` when it exists (set `MLB_ODDS_FRONTEND_DIST` to serve a build from
elsewhere); without a build, `/` returns JSON pointing at `/docs`.

## Circa Million consensus board (contest tool)

A separate FastAPI app for the Circa Million VIII pick'em workflow (see
`circa-million-2026-rules.md` for the contest and the plan; conventions in
D-020). It compares Circa's static contest spreads — entered by hand — against
the market consensus in the NFL odds database and reports the edge, movement
since entry, key-number crossings, and the week's pick deadline.

```bash
# collect NFL market lines (repeat Thu-Sat; the board reads this DB)
mlb-odds collect --once --sport nfl

# run the contest API (reads $NFL_ODDS_DB, owns $CONTEST_DB)
uvicorn mlb_odds.contest_api:app --port 8001
```

The contest UI lives at `/` (single static page, no build step): board with
edge/movement/countdown, blind proposal entry, the reveal with voting and the
working card, card lock + ETSN, and the season dashboard. Set
`CONTEST_MEMBERS=name1,name2,name3` (ordered — position defines the weekly
captain rotation).

API surface (Swagger at `/docs`):

- `GET /api/contest/board?week=N` — per-book spreads, consensus, contest
  line, edge (`contest - consensus`; positive = value on home), movement
  since entry, key numbers, early-kickoff flags, captain, booby-guard alert.
- `POST/GET /api/contest/lines` — manual Circa line entry (validated).
- `POST/GET /api/contest/proposals` — blind one-shot proposal sets (1-5
  picks); others' picks are hidden until your own are submitted (D-021).
- `POST /api/contest/votes`, `GET /api/contest/consensus` — the reveal:
  stances tallied into unanimous/majority/contested candidates, working card,
  Rule-8 effective deadline.
- `POST/PATCH/GET /api/contest/card` — lock the week's five (Rule 8 enforced
  at lock time), record the Circa ETSN.
- `POST /api/contest/results`, `GET /api/contest/season` — grading and the
  season dashboard (tiebreaker ladder, quarters, booby eligibility).

Same trust model as the odds API: no auth beyond the member list, keep it on
localhost/tailnet or behind your own front door.

## Docker + Tailscale

`docker-compose.yml` runs both APIs in containers, published **only** on the
host's Tailscale IP (plus loopback for the contest API) — reachable from your
tailnet via MagicDNS, invisible to every other network. Databases live in
`./data/` on the host, mounted at `/data`.

```bash
echo "TAILSCALE_IP=$(tailscale ip -4)" >> .env   # once
docker compose up -d --build
```

Then from any tailnet device:
- `http://workspace:8000` — MLB odds API + built web UI
- `http://workspace:8001/docs` — contest API (enter Circa lines here)

(Short `workspace` resolves on tailnet clients via the MagicDNS search domain;
the full `workspace.<tailnet>.ts.net` form works everywhere.)

NFL collection is a one-shot service kept out of `up` (metered credits, D-019
quota math). Run it manually or from cron, ~3x/day Thu–Sat during the season:

```bash
docker compose run --rm nfl-collect      # 3 credits per run
```

## Library

```python
from mlb_odds import OddsClient
from mlb_odds.providers import TheOddsAPI

client = OddsClient(providers=[TheOddsAPI()], db="odds.sqlite")

snapshot = client.fetch_and_store()   # poll all providers, persist, return models
games    = client.current_odds()      # latest stored snapshot per (game, provider)
closing  = client.closing_odds()      # last snapshot at/before each game's first pitch

df = client.history_df("2026-07-09-NYM-NYY-1")
# one row per (fetched_at, book, market, outcome) — ready for plotting line movement
df.pivot_table(index="fetched_at", columns="outcome", values="price")

flat = client.odds_df()               # all stored odds joined with game context
```

A provider raising doesn't abort the others: errors land in `client.last_errors`
and the cycle continues (see `docs/SPEC.md` FR1).

### Providers

Two ship with the library:

- `TheOddsAPI` — multi-book US lines; needs a key, metered (see quota math).
- `ESPN` — free, no key, unmetered; carries the single partner book ESPN
  surfaces (DraftKings at recording time), not a consensus (D-016). Unofficial
  endpoint — treat as best-effort.

```python
from mlb_odds.providers import ESPN, TheOddsAPI
client = OddsClient(providers=[TheOddsAPI(), ESPN()], db="odds.sqlite")
```

Cross-provider game identity converges on one canonical `game_id` (D-008), so
the same physical game's quotes line up regardless of source.

### Adding a provider

Implement the `OddsProvider` protocol — a `name` attribute and one method,
`fetch_game_lines() -> list[GameOdds]`, returning fully normalized models — and
pass an instance to `OddsClient`. No changes to storage, client, collector, or
CLI are needed; `tests/conftest.py`'s `FakeProvider` is the working proof.

## Development

```bash
uv sync
uv run pytest                            # offline — no API key, no network
uv run ruff check . && uv run mypy src/
```

Design docs: `docs/SPEC.md` (requirements + acceptance criteria),
`docs/ARCHITECTURE.md` (layout, models, schema), `docs/DECISIONS.md` (why),
`docs/ROADMAP.md` (progress).
