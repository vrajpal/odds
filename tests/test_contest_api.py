"""Contest API tests. Time-sensitive assertions are written against computed
contest facts (deadline instants, week_of(now)), never the wall clock's side
of them, so the suite passes before, during, and after the season."""

import os
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
    # C4.5 context: only one stored game, so neither team has a prior
    assert game["home_rest"] is None
    assert game["away_rest"] is None
    assert game["rest_differential"] is None
    assert game["divisional"] is True  # KC @ LAC, both AFC West


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


class TestSpreadHistory:
    """Chart endpoint: raw ticks + carry-forward consensus, same as-of math
    as the board so the two can never disagree."""

    def test_history_series_and_consensus(self, client, dbs):
        h = client.get(f"/api/contest/games/{dbs['game_id']}/spread-history").json()
        assert h["week"] == 1
        assert h["contest_line"] is None
        assert h["model_line"] is None  # one stored game: no rating fit
        assert [b["book"] for b in h["books"]].count("circa") == 2  # two snapshots
        # Consensus at T0: median(-2.5, -3.0); at T1: median(-3.5, -4.0).
        assert [p["spread"] for p in h["consensus"]] == [-2.75, -3.75]
        assert h["deadline"].startswith("2026-09-12T16:00")

    def test_history_includes_contest_line_once_entered(self, client, dbs):
        client.post(
            "/api/contest/lines",
            json={"week": 1, "game_id": dbs["game_id"], "home_spread": -2.5},
        )
        h = client.get(f"/api/contest/games/{dbs['game_id']}/spread-history").json()
        assert h["contest_line"] == -2.5
        assert h["line_entered_at"] is not None

    def test_unknown_and_malformed_game_404(self, client):
        assert client.get("/api/contest/games/2026-09-13-XX-YY-1/spread-history").status_code == 404
        assert client.get("/api/contest/games/not-a-game/spread-history").status_code == 404


class TestStatsEndpoints:
    """C4 stats endpoints: correct empty-season states and board enrichment."""

    def test_board_carries_context_and_model_fields(self, client, dbs):
        (game,) = client.get("/api/contest/board", params={"week": 1}).json()["games"]
        assert game["divisional"] is True  # KC @ LAC: both AFC West
        assert game["home_rest"] is None  # first stored game for both teams
        assert game["predicted_line"] is None  # one game: no rating fit yet

    def test_clv_empty_state(self, client, dbs):
        body = client.get("/api/contest/stats/clv").json()
        assert body == {"picks": [], "n": 0, "total_clv": 0.0, "avg_clv": None,
                        "positive": 0, "negative": 0}

    def test_calibration_empty_state(self, client, dbs):
        buckets = client.get("/api/contest/stats/calibration").json()
        assert len(buckets) == 5
        assert all(b["n"] == 0 and b["cover_rate"] is None for b in buckets)

    def test_ratings_404_below_minimum_games(self, client, dbs):
        assert client.get("/api/contest/stats/ratings").status_code == 404

    def test_member_stats_zeroed(self, client, dbs):
        stats = client.get("/api/contest/stats/members").json()
        assert [s["member"] for s in stats] == ["player1", "player2", "player3"]
        assert all(s["proposal_record"] == "0-0-0" for s in stats)


class TestContestMatchupLens:
    """Shared team lens on the contest app (D-034)."""

    @pytest.fixture
    def lens_client(self, dbs, monkeypatch):
        import json as _json

        import httpx as _httpx

        from conftest import FIXTURES
        from mlb_odds import contest_api as capi
        from mlb_odds import matchup as matchup_mod
        from mlb_odds.providers.espn import ESPN

        def handler(request: _httpx.Request) -> _httpx.Response:
            path = request.url.path
            if path.endswith("/nfl/teams"):
                payload = _json.loads((FIXTURES / "espn_nfl_teams.json").read_text())
            elif path.endswith("/statistics"):
                tid = path.split("/")[-2]
                payload = _json.loads(
                    (FIXTURES / f"espn_nfl_teamstats_{tid}.json").read_text()
                )
            else:
                tid = path.split("/")[-1]
                payload = _json.loads((FIXTURES / f"espn_nfl_team_{tid}.json").read_text())
            return _httpx.Response(200, json=payload)

        monkeypatch.setattr(
            capi, "ESPN",
            lambda sport="nfl": ESPN(sport=sport, transport=_httpx.MockTransport(handler)),
        )
        monkeypatch.setattr(matchup_mod, "_lens_cache", {})
        monkeypatch.setattr(matchup_mod, "_ids_cache", {})

        # Seed BUF @ KC (both teams have recorded stat fixtures: ids 2, 12).
        db = os.environ["NFL_ODDS_DB"]
        storage = Storage(db)
        go = make_nfl_spread_odds(
            {"circa": -3.0}, datetime(2026, 8, 1, tzinfo=UTC),
            away="BUF", home="KC",
            start_time=datetime(2026, 9, 13, 17, 0, tzinfo=UTC),
        )
        storage.store([go])
        storage.close()
        return TestClient(contest_api.app, raise_server_exceptions=False), go.game.game_id

    def test_contest_matchup_lens(self, lens_client):
        client, gid = lens_client
        body = client.get(f"/api/contest/games/{gid}/matchup").json()
        assert body["away_team"] == "BUF" and body["home_team"] == "KC"
        labels = [r["label"] for r in body["rows"]]
        assert "Points/G" in labels and "Sacks" in labels
        for r in body["rows"]:
            assert r["better"] in ("away", "home", None)

    def test_contest_matchup_unknown_game_404(self, lens_client):
        client, _ = lens_client
        assert client.get(
            "/api/contest/games/2026-09-13-XX-YY-1/matchup"
        ).status_code == 404
