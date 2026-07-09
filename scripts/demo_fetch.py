"""M1 demo: print today's normalized MLB odds from the live The Odds API.

Usage: THE_ODDS_API_KEY=... uv run python scripts/demo_fetch.py
Costs 3 API credits per run.
"""

import logging

from mlb_odds.providers import TheOddsAPI

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

provider = TheOddsAPI()
results = provider.fetch_game_lines()

print(f"\n{len(results)} games, quota remaining: {provider.quota_remaining}\n")
for go in sorted(results, key=lambda g: g.game.start_time):
    game = go.game
    print(f"{game.game_id}  ({game.start_time:%Y-%m-%d %H:%M} UTC)")
    books = sorted({q.book for q in go.quotes})
    for book in books:
        parts = []
        for market in ("moneyline", "run_line", "total"):
            quotes = [q for q in go.quotes if q.book == book and q.market == market]
            if not quotes:
                continue
            # A book can legitimately carry a partial outcome set (SPEC FR1);
            # skip the market rather than crash the whole board.
            fmt = {q.outcome: q for q in quotes}
            if market == "moneyline":
                away, home = fmt.get("away"), fmt.get("home")
                if away is not None and home is not None:
                    parts.append(f"ml {away.price:+d}/{home.price:+d}")
            elif market == "run_line":
                home = fmt.get("home")
                if home is not None and home.line is not None:
                    parts.append(f"rl {home.line:+.1f} ({home.price:+d})")
            else:
                over = fmt.get("over")
                if over is not None and over.line is not None:
                    parts.append(f"o/u {over.line:.1f} ({over.price:+d})")
        print(f"    {book:<16} {'  '.join(parts)}")
    print()
