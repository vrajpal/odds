"""Browser-level test for the Million page: a pick is shown as the team taken
and its Circa number ("KC -3"), never as a raw game id and "home"/"away" —
on the Consensus candidates, the resolver, the Card tab, and the vote
buttons. Same harness as test_survivor_ui.py; skips without Chromium."""

from datetime import timedelta

import httpx
import pytest

playwright = pytest.importorskip("playwright.sync_api")
from conftest import make_nfl_spread_odds  # noqa: E402
from mlb_odds.storage import Storage  # noqa: E402
from test_survivor_matrix import FETCH_AT, WEEK1  # noqa: E402
from test_survivor_ui import FROZEN_NOW, launch_browser, start_server  # noqa: E402


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
    # Plus a Wednesday-night game that has already kicked off at the frozen
    # Thursday-noon clock: it must not be a choice on the Propose tab.
    storage = Storage(tmp_path / "nfl-odds.sqlite")
    try:
        storage.store(
            [
                make_nfl_spread_odds(
                    {"circa": -1.0}, FETCH_AT, away="DAL", home="NYG", start_time=WEEK1
                ),
                make_nfl_spread_odds(
                    {"circa": -2.0},
                    FETCH_AT,
                    away="NO",
                    home="TB",
                    start_time=FROZEN_NOW - timedelta(hours=12),
                ),
            ]
        )
    finally:
        storage.close()
    dialogs: list[str] = []
    page.on("dialog", lambda d: dialogs.append(d.type))
    board = httpx.get(f"{server}/api/contest/board", params={"week": 1}).json()
    no_tb = next(g for g in board["games"] if g["home_team"] == "TB")
    assert no_tb["kicked_off"] is True
    games = [g for g in board["games"] if not g["kicked_off"]]
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
    # Propose tab: the kicked-off game shows a tag where the others show buttons.
    page.click('#tabs button[data-tab="propose"]')
    page.wait_for_selector("#propose-table tbody tr")
    assert page.locator(f'[data-pick="{no_tb["game_id"]}"]').count() == 0
    assert page.locator(f'[data-pick="{kc_lac["game_id"]}"]').count() == 3
    tb_row = page.locator("#propose-table tbody tr", has_text="NO @ TB")
    assert "kicked off" in tb_row.inner_text()
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


def test_history_page_lists_each_weeks_picks(page, server, tmp_path):
    from mlb_odds.contest import ContestStore

    storage = Storage(tmp_path / "nfl-odds.sqlite")  # the shared season has four week-1 games
    try:
        fifth = make_nfl_spread_odds(
            {"circa": -1.0}, FETCH_AT, away="DAL", home="NYG", start_time=WEEK1
        )
        storage.store([fifth])
    finally:
        storage.close()
    board = httpx.get(f"{server}/api/contest/board", params={"week": 1}).json()
    games = board["games"][:5]
    kc_lac = next(g for g in games if g["away_team"] == "KC")
    httpx.post(
        f"{server}/api/contest/lines",
        json={"week": 1, "game_id": kc_lac["game_id"], "home_spread": -3.0},
    )
    store = ContestStore(tmp_path / "contest.sqlite")
    store.lock_card(
        1,
        [(g["game_id"], "away" if g is kc_lac else "home") for g in games],
        locked_by="vijai",
        locked_at=FROZEN_NOW,
    )
    store.record_results(1, {kc_lac["game_id"]: "win"})
    store.close()

    page.goto(f"{server}/history.html")
    page.wait_for_selector('section.week[data-week="1"] tbody tr')
    rows = page.locator('section.week[data-week="1"] tbody tr')
    assert rows.count() == 5
    kc_row = page.locator(f'tr[data-game="{kc_lac["game_id"]}"]')
    text = kc_row.inner_text()
    assert "KC +3" in text and "win" in text  # the team taken and its number, never a raw id
    assert kc_lac["game_id"] not in text
    assert "1-0" in page.inner_text("h2")  # week record in the heading
    assert page.errors == []


def test_unsubmitted_proposal_survives_a_reload(browser, server):
    # Phone-sized, since that's where a dropped tab or a refresh bit (D-052).
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: d.accept())
    board = httpx.get(f"{server}/api/contest/board", params={"week": 1}).json()
    open_games = [g for g in board["games"] if not g["kicked_off"]]
    first, second = open_games[0]["game_id"], open_games[1]["game_id"]

    def open_propose():
        page.goto(f"{server}/")
        page.click('#tabs button[data-tab="propose"]')
        page.wait_for_selector("#propose-table tbody tr")

    def selected():
        return page.eval_on_selector_all(
            "#propose-table button.side.sel", "bs => bs.map(b => [b.dataset.pick, b.dataset.side])"
        )

    open_propose()
    page.click(f'[data-pick="{first}"][data-side="away"]')
    page.click(f'[data-pick="{second}"][data-side="pass"]')
    page.fill(f'[data-note="{first}"]', "line moved our way")
    page.reload()
    page.click('#tabs button[data-tab="propose"]')
    page.wait_for_selector("#propose-table button.side.sel")
    assert sorted(selected()) == sorted([[first, "away"], [second, "pass"]])
    assert page.input_value(f'[data-note="{first}"]') == "line moved our way"
    assert page.inner_text("#propose-msg") == "1 lean · 1 pass"

    # Submitting sends the restored draft and clears it.
    page.click("#propose-submit")
    page.wait_for_function("() => document.querySelector('#propose-submit').disabled")
    mine = httpx.get(
        f"{server}/api/contest/proposals", params={"week": 1, "member": "vijai"}
    ).json()["proposals"]
    assert {(p["game_id"], p["side"], p["note"]) for p in mine} == {
        (first, "away", "line moved our way"),
        (second, "pass", ""),
    }
    assert page.evaluate("() => localStorage.getItem('proposalDraft:vijai:1')") is None
    open_propose()
    assert selected() == []

    # A draft left on this device after submitting from another one is
    # dropped, not shown next to the real picks.
    page.select_option("#member", "sam")
    page.wait_for_selector(f'[data-pick="{first}"]')
    page.click(f'[data-pick="{first}"][data-side="home"]')
    page.fill(f'[data-note="{first}"]', "from the phone")
    assert page.evaluate("() => localStorage.getItem('proposalDraft:sam:1')") is not None
    r = httpx.post(
        f"{server}/api/contest/proposals",
        json={"week": 1, "member": "sam", "picks": [{"game_id": second, "side": "away"}]},
    )
    assert r.status_code == 201, r.text
    open_propose()
    page.wait_for_function("() => document.querySelector('#propose-submit').disabled")
    assert selected() == []
    assert page.input_value(f'[data-note="{first}"]') == ""
    assert page.evaluate("() => localStorage.getItem('proposalDraft:sam:1')") is None
    page.fill(f'[data-note="{first}"]', "typed after submitting")
    assert page.evaluate("() => localStorage.getItem('proposalDraft:sam:1')") is None
    assert errors == []
    context.close()


def test_switching_week_during_submit_keeps_the_next_weeks_draft(browser, server):
    # Submitting clears the submitted week's draft, not whatever week is on
    # screen when a slow POST returns (D-052).
    page = browser.new_page()
    page.on("dialog", lambda d: d.accept())
    week2 = httpx.get(f"{server}/api/contest/board", params={"week": 2}).json()
    g2 = next(g["game_id"] for g in week2["games"] if not g["kicked_off"])
    page.goto(f"{server}/")
    page.evaluate(
        "g => localStorage.setItem('proposalDraft:vijai:2',"
        " JSON.stringify({picks: {[g]: 'home'}, notes: {}}))",
        g2,
    )
    page.click('#tabs button[data-tab="propose"]')
    page.wait_for_selector("#propose-table [data-pick]")
    g1 = page.get_attribute("#propose-table [data-pick]", "data-pick")
    page.click(f'[data-pick="{g1}"][data-side="away"]')

    # Hold the POST's response in the page (a sync route handler that sleeps
    # would block this thread, so the week click would land after it).
    page.evaluate(
        """() => { const f = window.fetch;
        window.fetch = (u, o) => o?.method === "POST"
          ? f(u, o).then(r => new Promise(ok => setTimeout(() => ok(r), 1000))) : f(u, o); }"""
    )
    page.click("#propose-submit")
    page.click("#week-next")
    page.wait_for_function("() => document.querySelector('#hdr-week').value === '2'")
    page.wait_for_function("() => localStorage.getItem('proposalDraft:vijai:1') === null")
    assert page.evaluate("() => localStorage.getItem('proposalDraft:vijai:2')") is not None
    assert page.eval_on_selector_all(
        "#propose-table button.side.sel", "bs => bs.map(b => [b.dataset.pick, b.dataset.side])"
    ) == [[g2, "home"]]
    page.close()


def test_stale_table_saves_and_submits_for_its_own_week_and_member(browser, server):
    # After a week or member switch whose board never loads (a phone with no
    # signal), the table still on screen is the old one; edits and a submit
    # from it belong to its own week and member, not the switched-to ones (D-052).
    page = browser.new_page()
    page.on("dialog", lambda d: d.accept())
    page.goto(f"{server}/")
    page.click('#tabs button[data-tab="propose"]')
    page.wait_for_selector("#propose-table [data-pick]")
    g1 = page.get_attribute("#propose-table [data-pick]", "data-pick")
    page.click(f'[data-pick="{g1}"][data-side="away"]')
    page.evaluate(
        """() => { const f = window.fetch;
        window.fetch = (u, o) => String(u).includes("/api/contest/board")
          ? Promise.reject(new Error("offline")) : f(u, o); }"""
    )
    page.click("#week-next")
    page.fill(f'[data-note="{g1}"]', "still week 1")
    stored = page.evaluate("() => JSON.parse(localStorage.getItem('proposalDraft:vijai:1'))")
    assert stored == {"picks": {g1: "away"}, "notes": {g1: "still week 1"}}
    assert page.evaluate("() => localStorage.getItem('proposalDraft:vijai:2')") is None

    page.select_option("#member", "sam")
    page.click("#propose-submit")
    page.wait_for_function("() => localStorage.getItem('proposalDraft:vijai:1') === null")
    status = httpx.get(
        f"{server}/api/contest/proposals", params={"week": 1, "member": "vijai"}
    ).json()
    assert status["submitted"] == ["vijai"]
    assert [(p["game_id"], p["side"], p["note"]) for p in status["proposals"]] == [
        (g1, "away", "still week 1")
    ]
    page.close()
