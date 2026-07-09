"""Polling loop used by `mlb-odds collect`.

A plain loop, no scheduler dependency: fetch_and_store, log a cycle summary,
sleep, repeat. Real scheduling (cron/systemd) is the caller's job — `once=True`
does a single cycle and exits.
"""

import logging
import signal
import threading
from types import FrameType

from mlb_odds.client import OddsClient

logger = logging.getLogger("mlb_odds.collector")


def run(
    client: OddsClient,
    interval: float = 300.0,
    *,
    once: bool = False,
    stop: threading.Event | None = None,
) -> None:
    """Poll all providers every `interval` seconds until stopped.

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
