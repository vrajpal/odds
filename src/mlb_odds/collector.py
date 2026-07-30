"""Polling loop used by `mlb-odds collect`.

A plain loop, no scheduler dependency: fetch_and_store, log a cycle summary,
sleep, repeat. Real scheduling (cron/systemd) is the caller's job — `once=True`
does a single cycle and exits.
"""

import logging
import signal
import threading
from datetime import UTC, datetime, timedelta
from types import FrameType

from mlb_odds.client import OddsClient
from mlb_odds.models import Game

logger = logging.getLogger("mlb_odds.collector")

# A game's "live window" for --live mode (D-017): poll from shortly before
# first pitch until the game is plausibly over. 4h covers ~97% of MLB games
# post-pitch-clock; a stray extra-innings marathon just loses its tail.
LIVE_LEAD = timedelta(minutes=15)
LIVE_TAIL = timedelta(hours=4)
# With no window open (or an empty slate), re-check this often.
SLATE_RECHECK = 1800.0
# How far ahead to look for upcoming games when narrowing the slate query.
SLATE_HORIZON = timedelta(hours=36)


def seconds_until_live(games: list[Game], now: datetime) -> float | None:
    """0 if any game's live window contains `now`; else seconds until the next
    window opens; None if no window is open or ahead."""
    best: float | None = None
    for game in games:
        opens = game.start_time - LIVE_LEAD
        closes = game.start_time + LIVE_TAIL
        if opens <= now < closes:
            return 0.0
        if now < opens:
            wait = (opens - now).total_seconds()
            best = wait if best is None else min(best, wait)
    return best


def run(
    client: OddsClient,
    interval: float = 300.0,
    *,
    once: bool = False,
    live: bool = False,
    stop: threading.Event | None = None,
) -> None:
    """Poll all providers every `interval` seconds until stopped.

    `live=True` gates polling on the stored slate: cycles run only while some
    game is inside its live window [start-15m, start+4h); otherwise the loop
    idles (interruptibly) until the next window opens. The slate comes from
    the database, so run a normal collect first to learn today's games.

    SIGINT sets the stop event, so the loop exits cleanly even mid-sleep
    (the sleep is an interruptible Event.wait, not time.sleep). `stop` is
    injectable for tests.
    """
    stop_event = stop if stop is not None else threading.Event()

    def _on_sigint(signum: int, frame: FrameType | None) -> None:
        logger.info("interrupt received, stopping after this cycle/sleep")
        stop_event.set()

    try:
        previous = signal.signal(signal.SIGINT, _on_sigint)
    except ValueError:  # not in the main thread (e.g. some test runners)
        previous = None

    try:
        while not stop_event.is_set():
            if live:
                now = datetime.now(UTC)
                slate = client.games(window=(now - LIVE_TAIL, now + SLATE_HORIZON))
                wait = seconds_until_live(slate, now)
                if wait is None:
                    logger.info(
                        "live mode: no games in or approaching a live window; "
                        "rechecking the slate in %.0fs",
                        SLATE_RECHECK,
                    )
                    if stop_event.wait(SLATE_RECHECK):
                        break
                    continue
                if wait > 0:
                    logger.info("live mode: next live window opens in %.0fs", wait)
                    if stop_event.wait(min(wait, SLATE_RECHECK)):
                        break
                    continue
            _cycle(client)
            if once:
                break
            if stop_event.wait(interval):
                break
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def _cycle(client: OddsClient) -> None:
    results = client.fetch_and_store()
    rows = sum(len(go.quotes) for go in results)
    quota_parts = []
    for provider in client.providers:
        remaining = getattr(provider, "quota_remaining", None)
        quota_parts.append(f"{provider.name}={remaining if remaining is not None else '?'}")
    logger.info(
        "cycle: %d games, %d odds rows, %d provider error(s); quota remaining: %s",
        len(results),
        rows,
        len(client.last_errors),
        ", ".join(quota_parts) or "n/a",
    )
