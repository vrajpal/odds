# mlb-odds

Python library that fetches, normalizes, and stores MLB betting odds (moneyline,
run line, totals) from pluggable providers into SQLite, with a CLI, a polling
collector, and pandas access.

## Reference docs — read before building

- `docs/SPEC.md` — requirements and the v1 acceptance criteria (the definition of done)
- `docs/ARCHITECTURE.md` — package layout, domain models, provider protocol, SQL schema
- `docs/DECISIONS.md` — why things are the way they are; append, never rewrite
- `docs/ROADMAP.md` — milestone checklists; check items off as they land

When implementation reality contradicts a doc, update the doc in the same change —
these files are the source of truth, not a snapshot.

## Conventions

- Python ≥ 3.11, `src/` layout, `uv` for env/deps, hatchling build.
- Deps: httpx, pydantic v2, typer, pandas. Don't add runtime deps without a
  DECISIONS.md entry.
- Dependency direction: `cli → collector → client → (providers, storage) → models/teams`.
  Providers and storage never import each other.
- UTC everywhere internally; local time only at the CLI display layer.
- American odds (int) canonical; decimal derived.
- Lint/format `ruff`, types `mypy src/`, tests `pytest`. No live network in tests —
  providers are tested against fixtures in `tests/fixtures/`.

## Commands

```bash
uv sync                          # install deps
uv run pytest                    # tests (offline)
uv run ruff check . && uv run mypy src/
uv run mlb-odds collect --once   # needs THE_ODDS_API_KEY
```

## Environment

- `THE_ODDS_API_KEY` — The Odds API key (free tier: 500 credits/mo; one game-lines
  poll = 3 credits, so budget ~5 polls/day on free tier)
- `MLB_ODDS_DB` — SQLite path (default `./odds.sqlite`)
- `MLB_ODDS_FRONTEND_DIST` — directory the API serves at `/` (default
  `frontend/dist`; if it doesn't exist, `/` returns a JSON pointer to `/docs`)
- The CLI also loads both from a `.env` in the working directory (real env vars
  win); the library never reads `.env` (D-011)
