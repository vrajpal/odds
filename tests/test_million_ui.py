"""Browser-level test for the Million page: a pick is shown as the team taken
and its Circa number ("KC -3"), never as a raw game id and "home"/"away" —
on the Consensus candidates, the resolver, the Card tab, and the vote
buttons. Same harness as test_survivor_ui.py; skips without Chromium."""

import httpx
import pytest

playwright = pytest.importorskip("playwright.sync_api")
from conftest import make_nfl_spread_odds  # noqa: E402
from mlb_odds.storage import Storage  # noqa: E402
from test_survivor_matrix import FETCH_AT, WEEK1  # noqa: E402
from test_survivor_ui import launch_browser, start_server  # noqa: E402


@pytest.fixture(scope="module")
def browser():
    yield from launch_browser()


@pytest.fixture
def server(tmp_path, monkeypatch):
    yield from start_server(tmp_path, monkeypatch)


@pytest.fixture
def page(browser, server):
    context = browser.new_context(viewport={"width": 1300, "height": 900})
    pg = context.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("dialog", lambda d: d.accept())
    pg.errors = errors  # type: ignore[attr-defined]
    yield pg
    context.close()


def test_picks_read_as_team_and_number_everywhere(page, server, tmp_path):
    # The shared season has four week-1 games; a card needs five.
    storage = Storage(tmp_path / "nfl-odds.sqlite")
    try:
        storage.store(
            [
                make_nfl_spread_odds(
                    {"circa": -1.0}, FETCH_AT, away="DAL", home="NYG", start_time=WEEK1
                )
            ]
        )
    finally:
        storage.close()
    dialogs: list[str] = []
    page.on("dialog", lambda d: dialogs.append(d.type))
    board = httpx.get(f"{server}/api/contest/board", params={"week": 1}).json()
    games = board["games"]
    kc_lac = next(g for g in games if g["away_team"] == "KC" and g["home_team"] == "LAC")
    assert (
        httpx.post(
            f"{server}/api/contest/lines",
            json={"week": 1, "game_id": kc_lac["game_id"], "home_spread": -3.0},
        ).status_code
        == 201
    )
    five = [g["game_id"] for g in games[:5]]
    assert kc_lac["game_id"] in five
    for m in ("vijai", "sam", "alex"):
        r = httpx.post(
            f"{server}/api/contest/proposals",
            json={
                "week": 1,
                "member": m,
                "picks": [
                    {"game_id": kc_lac["game_id"], "side": "away"},
                    *[{"game_id": g, "side": "home"} for g in five if g != kc_lac["game_id"]],
                ],
            },
        )
        assert r.status_code == 201, r.text

    page.goto(f"{server}/")
    page.wait_for_selector("#board-table tbody tr")
    page.click('#tabs button[data-tab="consensus"]')
    page.wait_for_selector("#consensus-body table")
    body = page.inner_text("#consensus-body")
    assert "KC +3" in body  # away side of a -3 home line, as a person reads it
    assert "KC @ LAC" in body
    assert kc_lac["game_id"] not in body  # no raw ids on the consensus tab
    # Vote buttons carry team codes, not home/away.
    labels = page.locator(f'[data-vote="{kc_lac["game_id"]}"]').all_inner_texts()
    assert labels == ["KC", "LAC", "pass"]

    page.click("#lock-card")  # confirm auto-accepted (before the deadline)
    page.wait_for_selector("#card-body .card-box table")
    card = page.inner_text("#card-body")
    assert "KC +3" in card and "KC @ LAC" in card
    assert "pending" in card and kc_lac["game_id"] not in card
    # The lock confirm, plus the late-record prompt once the real clock is past
    # week 1's deadline (the UI compares to Date.now()); never an error alert.
    assert "alert" not in dialogs and dialogs[0] == "confirm"
    api_card = httpx.get(f"{server}/api/contest/card", params={"week": 1}).json()
    assert {p["game_id"] for p in api_card["picks"]} == set(five)
    assert page.errors == []
