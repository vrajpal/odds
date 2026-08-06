"""Contest API tests. Time-sensitive assertions are written against computed
contest facts (deadline instants, week_of(now)), never the wall clock's side
of them, so the suite passes before, during, and after the season."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import make_nfl_spread_odds
from mlb_odds import contest, contest_api
from mlb_odds.storage import Storage

# Fetch timestamps deliberately precede any real "now" this suite can run at
# (entered_at is server-now, and the movement baseline is consensus as of
# entry — seeding fetches after now would leave every entry baseline empty).
# fetched_at is independent of kickoff, so early timestamps are legal data.
T0 = datetime(2026, 8, 1, 17, 0, tzinfo=UTC)
T1 = datetime(2026, 8, 2, 18, 0, tzinfo=UTC)


@pytest.fixture
def dbs(tmp_path, monkeypatch):
    nfl_db = tmp_path / "nfl-odds.sqlite"
    storage = Storage(nfl_db)
    game_id = ""
    for fetched_at, lines in [
        (T0, {"circa": -2.5, "draftkings": -3.0}),
        (T1, {"circa": -3.5, "draftkings": -4.0}),
    ]:
        go = make_nfl_spread_odds(lines, fetched_at)
        storage.store([go], changed_only=True)
        game_id = go.game.game_id
    storage.close()
    monkeypatch.setenv("NFL_ODDS_DB", str(nfl_db))
    monkeypatch.setenv("CONTEST_DB", str(tmp_path / "contest.sqlite"))
    return {"nfl_db": nfl_db, "game_id": game_id}


@pytest.fixture
def client(dbs):
    return TestClient(contest_api.app, raise_server_exceptions=False)


def test_board_without_lines_shows_market_context(client, dbs):
    body = client.get("/api/contest/board", params={"week": 1}).json()
    assert body["week"] == 1
    assert body["deadline"] == contest.pick_deadline(1).isoformat()
    assert body["lines_post"] == contest.lines_post_time(1).isoformat()
    # Never assert the clock's sign — only that the two fields agree.
    assert body["locked"] == (body["seconds_to_deadline"] <= 0)
    (game,) = body["games"]
    assert game["game_id"] == dbs["game_id"]
    assert game["books"] == {"circa": -3.5, "draftkings": -4.0}
    assert game["consensus"] == -3.75
    assert game["contest_line"] is None
    assert game["edge"] is None


def test_enter_line_then_board_computes_edge(client, dbs):
    resp = client.post(
        "/api/contest/lines",
        json={"week": 1, "game_id": dbs["game_id"], "home_spread": -2.5},
    )
    assert resp.status_code == 201

    (game,) = client.get("/api/contest/board", params={"week": 1}).json()["games"]
    assert game["contest_line"] == -2.5
    assert game["edge"] == 1.25  # -2.5 - (-3.75)
    assert game["value_side"] == "home"
    assert game["key_numbers"] == [-3.0]


def test_movement_since_entry_tracks_post_entry_snapshots(client, dbs):
    # Enter the line now (both seeded snapshots are in the past, so the entry
    # baseline is today's consensus), then append a *later* snapshot. The
    # board's movement must be exactly the post-entry drift.
    client.post(
        "/api/contest/lines",
        json={"week": 1, "game_id": dbs["game_id"], "home_spread": -2.5},
    )
    storage = Storage(dbs["nfl_db"])
    storage.store(
        [
            make_nfl_spread_odds(
                {"circa": -5.5, "draftkings": -6.0}, datetime.now(UTC) + timedelta(hours=1)
            )
        ]
    )
    storage.close()

    (game,) = client.get("/api/contest/board", params={"week": 1}).json()["games"]
    assert game["consensus"] == -5.75
    assert game["movement_since_entry"] == -2.0  # -5.75 now vs -3.75 at entry
    assert game["edge"] == 3.25  # edge always uses current consensus


def test_line_upsert_overwrites(client, dbs):
    for spread in (-2.5, -3.0):
        client.post(
            "/api/contest/lines",
            json={"week": 1, "game_id": dbs["game_id"], "home_spread": spread},
        )
    (line,) = client.get("/api/contest/lines", params={"week": 1}).json()
    assert line["home_spread"] == -3.0


def test_line_rejects_unknown_game_and_wrong_week(client, dbs):
    unknown = client.post(
        "/api/contest/lines",
        json={"week": 1, "game_id": "2026-09-13-XX-YY-1", "home_spread": -2.5},
    )
    assert unknown.status_code == 404
    # Real game, but not in week 2's window.
    wrong_week = client.post(
        "/api/contest/lines",
        json={"week": 2, "game_id": dbs["game_id"], "home_spread": -2.5},
    )
    assert wrong_week.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"week": 0, "game_id": "g", "home_spread": -2.5},
        {"week": 19, "game_id": "g", "home_spread": -2.5},
        {"week": 1, "game_id": "g", "home_spread": -3.25},
        {"week": 1, "game_id": "g", "home_spread": -31.0},
    ],
)
def test_line_validation_422(client, body):
    assert client.post("/api/contest/lines", json=body).status_code == 422


def test_board_default_week_matches_clock(client):
    current = contest.week_of(datetime.now(UTC))
    resp = client.get("/api/contest/board")
    if current is None:
        assert resp.status_code == 400
    else:
        assert resp.status_code == 200
        assert resp.json()["week"] == current


def test_board_empty_week_still_returns_schedule(client):
    body = client.get("/api/contest/board", params={"week": 3}).json()
    assert body["games"] == []
    assert body["deadline"] == contest.pick_deadline(3).isoformat()


def test_missing_nfl_db_is_503_and_writes_nothing(tmp_path, monkeypatch):
    missing = tmp_path / "nope" / "nfl-odds.sqlite"
    monkeypatch.setenv("NFL_ODDS_DB", str(missing))
    monkeypatch.setenv("CONTEST_DB", str(tmp_path / "contest.sqlite"))
    client = TestClient(contest_api.app, raise_server_exceptions=False)
    assert client.get("/api/contest/board", params={"week": 1}).status_code == 503
    # Read-only open must not have created the collector's database.
    assert not missing.exists()


def test_board_week_out_of_range_422(client):
    assert client.get("/api/contest/board", params={"week": 19}).status_code == 422
