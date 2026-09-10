"""Browser-level tests for survivor.html's Matrix tab (D-041).

The tab's planning logic — rank-by-leg, safe-leg counts, best open leg,
used-team dimming, the market/model toggle — lives in inline JavaScript that
no Python test can reach. These drive the real page in headless Chromium
against a real uvicorn server on a loopback port, with every expectation
derived from the same API payload the page fetched, so they pin behavior
without hardcoding numbers.

Skips cleanly when Playwright or its Chromium build is absent
(`uv run playwright install chromium` to enable locally)."""

import socket
import threading
import time
from datetime import UTC, datetime

import httpx
import pytest
import uvicorn

from mlb_odds import contest_api
from test_survivor_matrix import build_season

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

FROZEN_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
LEG_COUNT = 20
SUMMARY_COLUMNS = 2  # safe, best


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except PlaywrightError as exc:  # no browser build on this machine
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


@pytest.fixture
def server(tmp_path, monkeypatch):
    """The contest app on a free loopback port, in a daemon thread."""
    build_season(tmp_path / "nfl-odds.sqlite")
    monkeypatch.setenv("NFL_ODDS_DB", str(tmp_path / "nfl-odds.sqlite"))
    monkeypatch.setenv("CONTEST_DB", str(tmp_path / "contest.sqlite"))
    monkeypatch.setenv("CONTEST_MEMBERS", "vijai,sam,alex")
    monkeypatch.setattr(contest_api, "_now", lambda: FROZEN_NOW)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(
        uvicorn.Config(contest_api.app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert srv.started, "uvicorn did not start"
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(10)


@pytest.fixture
def page(browser, server):
    context = browser.new_context(viewport={"width": 1400, "height": 1000})
    pg = context.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.goto(f"{server}/survivor.html")
    pg.wait_for_selector("#board-table tbody tr")
    pg.errors = errors  # type: ignore[attr-defined]
    yield pg
    context.close()


def open_matrix(pg) -> None:
    pg.click('#tabs button[data-tab="matrix"]')
    pg.wait_for_selector("#mx-table thead th")


def team_order(pg) -> list[str]:
    cells = pg.locator("#mx-table tbody tr td.team").all_inner_texts()
    return [c.split()[0].rstrip("🦃🎄") for c in cells]


def cell_number(pg, team: str, leg_id: str) -> int | None:
    """The printed percentage in a team's leg cell (None for a bye)."""
    row = pg.locator("#mx-table tbody tr", has=pg.locator(f'td.team:has-text("{team}")')).first
    # Header and body share column positions: team is column 0 in both.
    col = [th.get_attribute("data-sort") for th in pg.locator("#mx-table thead th").all()].index(
        leg_id
    )
    td = row.locator("td").nth(col)
    text = td.locator(".p").inner_text() if td.locator(".p").count() else "–"
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def api_matrix(server) -> dict:
    return httpx.get(f"{server}/api/survivor/matrix").json()


def lock_pick(server, leg: str, team: str) -> None:
    for member in ("vijai", "sam", "alex"):
        r = httpx.post(
            f"{server}/api/survivor/proposal",
            json={"leg": leg, "member": member, "choices": [{"team": team}]},
        )
        assert r.status_code == 201, r.text
    r = httpx.post(
        f"{server}/api/survivor/pick", json={"leg": leg, "member": "vijai", "team": team}
    )
    assert r.status_code == 201, r.text


def test_matrix_renders_every_team_and_leg_from_the_api(page, server):
    open_matrix(page)
    body = api_matrix(server)
    assert page.locator("#mx-table tbody tr").count() == 32
    assert page.locator("#mx-table thead th").count() == 1 + LEG_COUNT + SUMMARY_COLUMNS
    assert team_order(page) == sorted(t["team"] for t in body["teams"])  # alphabetical by default
    teams = {t["team"]: t for t in body["teams"]}
    kc = teams["KC"]["cells"]["1"]
    assert cell_number(page, "KC", "1") == round(kc["market_win_prob"] * 100)
    kc_row = page.locator("#mx-table tbody tr", has=page.locator('td.team:has-text("KC")')).first
    assert "@LAC" in kc_row.inner_text()  # away game is written @opponent
    assert cell_number(page, "KC", "4") is None  # bye
    # Current-leg column is marked; legend explains the fill and markers.
    assert page.locator('#mx-table thead th.cur[data-sort="1"]').count() == 1
    assert "toss-up" in page.locator("#mx-legend").inner_text()
    assert page.errors == []


def test_clicking_a_leg_ranks_teams_with_byes_last_then_restores(page, server):
    open_matrix(page)
    body = api_matrix(server)
    teams = {t["team"]: t for t in body["teams"]}
    page.click('#mx-table th[data-sort="2"]')
    order = team_order(page)
    played = [t for t in order if "2" in teams[t]["cells"]]
    byes = [t for t in order if "2" not in teams[t]["cells"]]
    assert order == played + byes  # everyone with a game before every bye
    probs = [teams[t]["cells"]["2"]["market_win_prob"] for t in played]
    assert probs == sorted(probs, reverse=True)
    assert byes == sorted(byes)
    assert "ranked by Week 2" in page.locator("#mx-note").inner_text()
    assert page.locator('#mx-table th.sorted[data-sort="2"]').count() == 1
    page.click('#mx-table th[data-sort="2"]')  # second click: back to alphabetical
    assert team_order(page) == sorted(teams)
    assert page.locator("#mx-table th.sorted").count() == 0
    assert page.errors == []


def test_model_toggle_and_safe_threshold_follow_the_api(page, server):
    open_matrix(page)
    teams = {t["team"]: t for t in api_matrix(server)["teams"]}
    lac = teams["LAC"]["cells"]
    assert cell_number(page, "LAC", "1") == round(lac["1"]["market_win_prob"] * 100)
    page.select_option("#mx-src", "model")
    assert cell_number(page, "LAC", "1") == round(lac["1"]["model_win_prob"] * 100)
    assert "model" in page.locator("#mx-legend").inner_text()

    def safe_column(team: str) -> str:
        row = page.locator(
            "#mx-table tbody tr", has=page.locator(f'td.team:has-text("{team}")')
        ).first
        return row.locator("td").nth(1 + LEG_COUNT).inner_text()

    def expected_safe(team: str, threshold: float, key: str) -> int:
        return sum(1 for c in teams[team]["cells"].values() if c[key] >= threshold)

    assert safe_column("LAC") == str(expected_safe("LAC", 0.65, "model_win_prob"))
    page.select_option("#mx-safe", "75")
    assert safe_column("LAC") == str(expected_safe("LAC", 0.75, "model_win_prob"))
    page.select_option("#mx-src", "market")
    page.select_option("#mx-safe", "60")
    assert safe_column("LAC") == str(expected_safe("LAC", 0.60, "market_win_prob"))
    # best = the strongest open leg, written as "NN Wk".
    best = max(lac.items(), key=lambda kv: kv[1]["market_win_prob"])
    row = page.locator("#mx-table tbody tr", has=page.locator('td.team:has-text("LAC")')).first
    best_text = row.locator("td").nth(2 + LEG_COUNT).inner_text()
    assert best_text.split()[0] == str(round(best[1]["market_win_prob"] * 100))
    assert best_text.split()[1] == "W" + best[0]
    assert page.errors == []


def test_locked_pick_dims_the_team_and_hide_used_removes_it(page, server):
    lock_pick(server, "1", "LAC")
    page.reload()
    page.wait_for_selector("#board-table tbody tr")
    open_matrix(page)
    lac = page.locator("#mx-table tbody tr.used", has=page.locator('td.team:has-text("LAC")'))
    assert lac.count() == 1
    picked = lac.locator("td.picked")
    assert picked.count() == 1 and "✓" in picked.inner_text()
    assert "LAC" in page.locator('#mx-table thead th[data-sort="1"]').inner_text()
    # Summary columns are blank for a burned team.
    assert lac.locator("td").nth(1 + LEG_COUNT).inner_text() == "–"
    # Ranking a leg sinks the used team to the bottom.
    page.click('#mx-table th[data-sort="2"]')
    assert team_order(page)[-1] == "LAC"
    page.check("#mx-hide-used")
    assert page.locator("#mx-table tbody tr").count() == 31
    assert "LAC" not in team_order(page)
    page.uncheck("#mx-hide-used")
    assert page.locator("#mx-table tbody tr").count() == 32
    assert page.errors == []


def test_matrix_reloads_after_a_pick_lands_elsewhere(page, server):
    """refresh() invalidates the cached payload so a lock made on another
    tab (or by another member) shows up without a page reload."""
    open_matrix(page)
    assert page.locator("#mx-table tbody tr.used").count() == 0
    lock_pick(server, "1", "SEA")
    page.click('#tabs button[data-tab="plan"]')  # any tab switch...
    page.click('#tabs button[data-tab="board"]')
    page.wait_for_timeout(100)
    page.evaluate("refresh()")  # ...then the app's own refresh cycle
    page.wait_for_timeout(300)
    open_matrix(page)
    page.wait_for_selector("#mx-table tbody tr.used")
    assert team_order(page).index("SEA") >= 0
    assert page.locator("#mx-table tbody tr.used td.team").inner_text().startswith("SEA")
    assert page.errors == []
