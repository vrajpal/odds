"""Cross-cutting integration: the Million and Survivor tools sharing one
process and one state file, the collector write path feeding the survivor
read path, and the static UI mount coexisting with both APIs.

Everything runs through the public surfaces (OddsClient, the FastAPI app) —
no reaching into module internals beyond the sanctioned _now seam."""

import sqlite3
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from conftest import FakeProvider, make_nfl_spread_odds
from mlb_odds import contest, contest_api, survivor
from mlb_odds.client import OddsClient
from mlb_odds.storage import Storage

FROZEN_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)  # Thu of contest week 1
FETCH_AT = datetime(2026, 8, 1, 17, 0, tzinfo=UTC)
SUNDAY = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)
MATCHUPS = [("KC", "LAC"), ("BUF", "MIA"), ("DAL", "NYG"), ("SF", "SEA"), ("GB", "CHI")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    nfl_db = tmp_path / "nfl-odds.sqlite"
    storage = Storage(nfl_db)
    ids = {}
    for away, home in MATCHUPS:
        go = make_nfl_spread_odds(
            {"circa": -3.0, "draftkings": -2.5}, FETCH_AT, away=away, home=home,
            start_time=SUNDAY,
        )
        storage.store([go])
        ids[f"{away}@{home}"] = go.game.game_id
    storage.close()
    monkeypatch.setenv("NFL_ODDS_DB", str(nfl_db))
    monkeypatch.setenv("CONTEST_DB", str(tmp_path / "contest.sqlite"))
    monkeypatch.setenv("CONTEST_MEMBERS", "vijai,sam,alex")
    monkeypatch.delenv("CONTEST_MEMBER_EMAILS", raising=False)
    monkeypatch.setattr(contest_api, "_now", lambda: FROZEN_NOW)
    return {"ids": ids, "nfl_db": nfl_db, "contest_db": tmp_path / "contest.sqlite"}


@pytest.fixture
def client(env):
    return TestClient(contest_api.app, raise_server_exceptions=False)


# --- one state file, two tools -----------------------------------------------


def test_million_and_survivor_flows_share_one_state_file(client, env):
    """A full Million week and a Survivor pick, interleaved through the same
    app: both persist to contest.sqlite (separate migration chains), neither
    tool's endpoints see the other's state, and both season views are right."""
    # Million: enter a line, lock the week-1 card, grade it.
    client.post(
        "/api/contest/lines",
        json={"week": 1, "game_id": env["ids"]["KC@LAC"], "home_spread": -2.5},
    )
    picks = [{"game_id": gid, "side": "home"} for gid in env["ids"].values()]
    r = client.post(
        "/api/contest/card", json={"week": 1, "member": "vijai", "picks": picks}
    )
    assert r.status_code == 201
    client.post(
        "/api/contest/results",
        json={
            "week": 1,
            "results": {
                env["ids"]["KC@LAC"]: "win",
                env["ids"]["BUF@MIA"]: "win",
                env["ids"]["DAL@NYG"]: "win",
                env["ids"]["SF@SEA"]: "loss",
                env["ids"]["GB@CHI"]: "push",
            },
        },
    )

    # Survivor: lock and grade a pick in the same conceptual week.
    client.post(
        "/api/survivor/pick", json={"leg": "1", "member": "vijai", "team": "LAC"}
    )
    client.post("/api/survivor/result", json={"leg": "1", "result": "win"})

    season = client.get("/api/contest/season").json()
    assert season["total_points"] == 3.5  # 3 wins + a push
    status = client.get("/api/survivor/status").json()
    assert status["entry"]["survived"] == 1
    assert status["used"] == {"LAC": "1"}

    # One file, both schema chains, no cross-contamination.
    conn = sqlite3.connect(env["contest_db"])
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert {"cards", "card_picks", "contest_lines", "schema_version"} <= tables
    assert {"survivor_picks", "survivor_proposals", "survivor_schema_version"} <= tables


@pytest.mark.parametrize("survivor_first", [True, False])
def test_store_migrations_are_order_independent(tmp_path, survivor_first):
    """Whichever tool touches a fresh contest.sqlite first, both migration
    chains land and both stores work — the two version tables never fight."""
    db = tmp_path / "contest.sqlite"
    makers = [
        lambda: contest.ContestStore(db).close(),
        lambda: survivor.SurvivorStore(db).close(),
    ]
    if survivor_first:
        makers.reverse()
    for make in makers:
        make()

    s = survivor.SurvivorStore(db)
    s.lock_pick("1", "LAC", "gid", locked_by="vijai", locked_at=FROZEN_NOW)
    s.close()
    c = contest.ContestStore(db)
    c.set_line(1, "gid", -2.5, entered_at=FROZEN_NOW)
    assert c.lines(1)["gid"].home_spread == -2.5
    c.close()
    s = survivor.SurvivorStore(db)
    assert s.used_teams() == {"LAC": "1"}
    s.close()


# --- collector write path -> survivor read path ------------------------------


def test_collector_pipeline_moves_the_survivor_board(env, client):
    """Odds ingested through the real write path (OddsClient + provider,
    changed-only) shift the survivor board's consensus and win probability —
    the whole pipeline a game-day line move actually takes."""
    board = client.get("/api/survivor/board", params={"leg": "1"}).json()
    game = {g["game_id"]: g for g in board["games"]}[env["ids"]["KC@LAC"]]
    assert game["consensus"] == -2.75  # seeded median
    wp_before = game["home_win_prob"]

    moved = make_nfl_spread_odds(
        {"circa": -6.5, "draftkings": -7.5},
        datetime(2026, 9, 9, 17, 0, tzinfo=UTC),
        away="KC", home="LAC", start_time=SUNDAY,
    )
    odds_client = OddsClient(
        [FakeProvider([moved])], env["nfl_db"], changed_only=True
    )
    try:
        assert len(odds_client.fetch_and_store()) == 1
    finally:
        odds_client.close()

    board = client.get("/api/survivor/board", params={"leg": "1"}).json()
    game = {g["game_id"]: g for g in board["games"]}[env["ids"]["KC@LAC"]]
    assert game["consensus"] == -7.0
    assert game["home_win_prob"] == survivor.win_probability(-7.0)
    assert game["home_win_prob"] > wp_before
    # The other games kept their carry-forward numbers — a one-game move
    # never disturbs the rest of the board.
    other = {g["game_id"]: g for g in board["games"]}[env["ids"]["BUF@MIA"]]
    assert other["consensus"] == -2.75


def test_provider_outage_leaves_board_serving_last_known(env, client):
    """A failed poll (ProviderError) writes nothing; the board still serves
    the last stored numbers — the outage mode the collector is built for."""
    from mlb_odds.providers.base import ProviderError

    broken = FakeProvider(error=ProviderError("feed down"))
    odds_client = OddsClient([broken], env["nfl_db"], changed_only=True)
    try:
        assert odds_client.fetch_and_store() == []
        assert "fake" in odds_client.last_errors
    finally:
        odds_client.close()

    board = client.get("/api/survivor/board", params={"leg": "1"}).json()
    game = {g["game_id"]: g for g in board["games"]}[env["ids"]["KC@LAC"]]
    assert game["consensus"] == -2.75


# --- one server, both UIs, both APIs -----------------------------------------


def test_static_pages_and_both_apis_coexist(client):
    """The static mount serves both tool pages without shadowing either API —
    the registration-order property the mount comment promises."""
    index = client.get("/")
    assert index.status_code == 200 and "Circa Million" in index.text
    surv = client.get("/survivor.html")
    assert surv.status_code == 200 and "Circa Survivor" in surv.text
    assert client.get("/api/contest/health").json() == {"status": "ok"}
    assert client.get("/api/survivor/status").status_code == 200
    # Cross-links exist so the two pages reach each other.
    assert "/survivor.html" in index.text
    assert 'href="/"' in surv.text
