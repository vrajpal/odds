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

## CLI

```bash
mlb-odds collect --once            # one fetch cycle, then exit (cron-friendly)
mlb-odds collect --interval 21600  # poll loop until Ctrl-C (clean SIGINT exit)

mlb-odds today                     # today's board from stored data — no network
mlb-odds history 2026-07-09-NYM-NYY-1        # line movement for one game
mlb-odds history NYM@NYY --date 2026-07-09   # same, fuzzy AWAY@HOME form
mlb-odds export --format csv --out odds.csv
mlb-odds export --format parquet --out odds.parquet   # needs pyarrow
```

All commands take `--db PATH` (or `MLB_ODDS_DB`). `today` and `history` display
times in your local timezone; storage is always UTC.

Example `today` output:

```
NYM @ NYY  2026-07-09 07:05 PM EDT  [2026-07-09-NYM-NYY-1]
  book              moneyline     run line        total
  draftkings        +120/-140     -1.5 (-105)     8.5 (o-108)
  fanduel           +118/-138     -1.5 (-102)     8.5 (o-110)
```

Cron example (5 polls/day, free-tier friendly):

```cron
0 10,13,16,19,22 * * *  THE_ODDS_API_KEY=... MLB_ODDS_DB=/home/me/odds.sqlite mlb-odds collect --once
```

## Library

```python
from mlb_odds import OddsClient
from mlb_odds.providers import TheOddsAPI

client = OddsClient(providers=[TheOddsAPI()], db="odds.sqlite")

snapshot = client.fetch_and_store()   # poll all providers, persist, return models
games    = client.current_odds()      # latest stored snapshot per (game, provider)

df = client.history_df("2026-07-09-NYM-NYY-1")
# one row per (fetched_at, book, market, outcome) — ready for plotting line movement
df.pivot_table(index="fetched_at", columns="outcome", values="price")

flat = client.odds_df()               # all stored odds joined with game context
```

A provider raising doesn't abort the others: errors land in `client.last_errors`
and the cycle continues (see `docs/SPEC.md` FR1).

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
