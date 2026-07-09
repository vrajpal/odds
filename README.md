# mlb-odds

Fetch, normalize, and store MLB betting odds (moneyline, run line, totals) from
pluggable providers, with SQLite snapshot history, a CLI, and pandas access.

**Status: pre-release, under active development.** See `docs/SPEC.md` for what v1
will include and `docs/ROADMAP.md` for progress.

## Development

```bash
uv sync
uv run pytest          # offline — no API key needed
```

Live fetching requires a key from [the-odds-api.com](https://the-odds-api.com)
in `THE_ODDS_API_KEY`. Free tier is 500 credits/month; one game-lines poll costs
3 credits (~5 polls/day).
