"""Survivor API: the blind propose -> reveal -> vote -> lock flow, hard rule
enforcement (used teams, deadlines, schedule validation), status/board views,
and ESPN auto-grading. Time-sensitive assertions run against a frozen _now."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from conftest import make_nfl_spread_odds
from mlb_odds import contest_api, survivor
from mlb_odds.providers.espn import ESPN
from mlb_odds.storage import Storage
from test_auto_grading import scoreboard_transport

FROZEN_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)  # Thu of contest week 1
FETCH_AT = datetime(2026, 8, 1, 17, 0, tzinfo=UTC)
WEEK1_SUNDAY = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)
WEEK2_SUNDAY = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)
THANKSGIVING = datetime(2026, 11, 26, 21, 30, tzinfo=UTC)  # Thu Nov 26, TG leg window


@pytest.fixture
def env(tmp_path, monkeypatch):
    nfl_db = tmp_path / "nfl-odds.sqlite"
    storage = Storage(nfl_db)
    ids = {}
    for away, home, start in [
        ("KC", "LAC", WEEK1_SUNDAY),
        ("SF", "SEA", WEEK1_SUNDAY),
        ("GB", "CHI", WEEK1_SUNDAY),
        ("LAC", "LV", WEEK2_SUNDAY),
        ("DET", "DAL", THANKSGIVING),
    ]:
        go = make_nfl_spread_odds(
            {"circa": -3.0, "draftkings": -4.0}, FETCH_AT, away=away, home=home,
            start_time=start,
        )
        storage.store([go])
        ids[f"{away}@{home}"] = go.game.game_id
    storage.close()
    monkeypatch.setenv("NFL_ODDS_DB", str(nfl_db))
    monkeypatch.setenv("CONTEST_DB", str(tmp_path / "contest.sqlite"))
    monkeypatch.setenv("CONTEST_MEMBERS", "vijai,sam,alex")
    monkeypatch.setattr(contest_api, "_now", lambda: FROZEN_NOW)
    return {"ids": ids, "contest_db": tmp_path / "contest.sqlite"}


@pytest.fixture
def client(env):
    return TestClient(contest_api.app, raise_server_exceptions=False)


# --- status & board ----------------------------------------------------------


def test_status_fresh_season(client):
    body = client.get("/api/survivor/status").json()
    assert body["current_leg"] == "1"
    assert body["entry"] == {"alive": True, "survived": 0, "reason": None, "at_leg": None}
    assert body["used"] == {}
    assert len(body["remaining_teams"]) == 32
    assert len(body["legs"]) == 20
    outlook = {o["leg_id"]: o for o in body["holiday_outlook"]}
    assert len(outlook["TG"]["remaining"]) == 10
    assert len(outlook["XMAS"]["remaining"]) == 8
    assert outlook["TG"]["danger"] == "none"


def test_board_defaults_to_current_leg_with_win_probs(client, env):
    body = client.get("/api/survivor/board").json()
    assert body["leg_id"] == "1"
    assert body["captain"] == "vijai"  # first member, first leg
    assert body["locked"] is (body["seconds_to_deadline"] <= 0)
    games = {g["game_id"]: g for g in body["games"]}
    assert set(games) == {
        env["ids"]["KC@LAC"], env["ids"]["SF@SEA"], env["ids"]["GB@CHI"]
    }
    g = games[env["ids"]["KC@LAC"]]
    assert g["consensus"] == -3.5  # median of -3.0 / -4.0
    assert g["home_win_prob"] == survivor.win_probability(-3.5)
    assert g["away_win_prob"] == pytest.approx(1 - g["home_win_prob"])
    assert g["home_used"] is None and g["away_used"] is None
    assert g["divisional"] is True  # KC @ LAC


def test_board_thanksgiving_leg_carries_the_rules_slate(client, env):
    body = client.get("/api/survivor/board", params={"leg": "TG"}).json()
    assert body["label"] == "Thanksgiving"
    assert len(body["holiday_slate"]) == 10
    (game,) = body["games"]
    assert game["game_id"] == env["ids"]["DET@DAL"]
    # Kicks before the Wed 4 PM deadline? No — Thanksgiving Thursday is after,
    # so this is not an early kickoff; the deadline governs.
    assert game["early_kickoff"] is False


def test_board_rejects_unknown_leg(client):
    assert client.get("/api/survivor/board", params={"leg": "42"}).status_code == 422


# --- blind proposals, reveal, votes ------------------------------------------


def test_blind_proposal_reveal_and_vote_flow(client):
    # vijai proposes blind; sam can't see it before submitting their own.
    r = client.post(
        "/api/survivor/proposal",
        json={"leg": "1", "member": "vijai", "choices": [{"team": "LAC", "note": "home number"}]},
    )
    assert r.status_code == 201
    view = client.get(
        "/api/survivor/proposals", params={"leg": "1", "member": "sam"}
    ).json()
    assert view["proposals"] == []  # blind
    assert view["waiting_on"] == ["sam", "alex"]

    # Reveal requires your own proposal in.
    r = client.get("/api/survivor/consensus", params={"leg": "1", "member": "sam"})
    assert r.status_code == 409

    client.post(
        "/api/survivor/proposal",
        json={"leg": "1", "member": "sam", "choices": [{"team": "KC"}]},
    )
    client.post(
        "/api/survivor/proposal",
        json={"leg": "1", "member": "alex", "choices": [{"team": "LAC"}]},
    )
    c = client.get(
        "/api/survivor/consensus", params={"leg": "1", "member": "sam"}
    ).json()
    assert c["captain"] == "vijai"
    assert [(x["team"], x["status"]) for x in c["candidates"]] == [
        ("LAC", "majority"),
        ("KC", "contested"),
    ]
    assert c["working_pick"]["team"] == "LAC"
    # Sunday kickoff is after the Saturday deadline -> deadline governs.
    assert c["effective_deadline"] == contest_api._pt(survivor.leg("1").deadline)

    # sam moves their backing: vote overrides the proposal -> unanimous.
    c = client.post(
        "/api/survivor/vote", json={"leg": "1", "member": "sam", "team": "LAC"}
    ).json()
    (only,) = c["candidates"]
    assert (only["team"], only["status"]) == ("LAC", "unanimous")


def test_proposal_is_one_shot_and_validates_team(client):
    client.post(
        "/api/survivor/proposal",
        json={"leg": "1", "member": "vijai", "choices": [{"team": "LAC"}]},
    )
    assert (
        client.post(
            "/api/survivor/proposal",
            json={"leg": "1", "member": "vijai", "choices": [{"team": "KC"}]},
        ).status_code
        == 409
    )
    # Unknown code -> 422; known code with no stored game this leg -> 404.
    assert (
        client.post(
            "/api/survivor/proposal",
            json={"leg": "1", "member": "sam", "choices": [{"team": "LAX"}]},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/survivor/proposal",
            json={"leg": "1", "member": "sam", "choices": [{"team": "NYJ"}]},
        ).status_code
        == 404
    )


# --- locking: the hard rules -------------------------------------------------


def test_lock_pick_burns_team_across_all_legs(client):
    r = client.post(
        "/api/survivor/pick", json={"leg": "1", "member": "vijai", "team": "LAC"}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["pick"]["team"] == "LAC"
    assert body["pick"]["effective_deadline"] == contest_api._pt(
        survivor.leg("1").deadline
    )

    # One pick per leg (Rule 18)...
    assert (
        client.post(
            "/api/survivor/pick", json={"leg": "1", "member": "vijai", "team": "KC"}
        ).status_code
        == 409
    )
    # ...and LAC is now burned for the season (Rule 15a): proposals and locks
    # in later legs both refuse it.
    assert (
        client.post(
            "/api/survivor/proposal",
            json={"leg": "2", "member": "sam", "choices": [{"team": "LAC"}]},
        ).status_code
        == 409
    )
    r = client.post(
        "/api/survivor/pick", json={"leg": "2", "member": "sam", "team": "LAC"}
    )
    assert r.status_code == 409
    assert "Rule 15a" in r.json()["detail"]

    status = client.get("/api/survivor/status").json()
    assert status["used"] == {"LAC": "1"}
    assert "LAC" not in status["remaining_teams"]
    legs = {lg["leg_id"]: lg for lg in status["legs"]}
    assert legs["1"]["pick"]["team"] == "LAC"


def test_lock_returns_holiday_burn_warnings(client):
    # GB is on both holiday slates; week-1 lock warns for each (info level).
    body = client.post(
        "/api/survivor/pick", json={"leg": "1", "member": "vijai", "team": "GB"}
    ).json()
    messages = [w["message"] for w in body["warnings"]]
    assert any("Thanksgiving" in m and "9 of 10" in m for m in messages)
    assert any("Christmas" in m and "7 of 8" in m for m in messages)


def test_lock_refused_after_deadline(client, monkeypatch):
    after = survivor.leg("1").deadline.astimezone(UTC)
    monkeypatch.setattr(contest_api, "_now", lambda: after)
    r = client.post(
        "/api/survivor/pick", json={"leg": "1", "member": "vijai", "team": "LAC"}
    )
    assert r.status_code == 409
    assert "deadline" in r.json()["detail"]


def test_vote_blocked_once_pick_locked(client):
    client.post(
        "/api/survivor/proposal",
        json={"leg": "1", "member": "vijai", "choices": [{"team": "LAC"}]},
    )
    client.post(
        "/api/survivor/pick", json={"leg": "1", "member": "vijai", "team": "LAC"}
    )
    r = client.post(
        "/api/survivor/vote", json={"leg": "1", "member": "vijai", "team": "KC"}
    )
    assert r.status_code == 409


# --- ETSN, results, elimination ----------------------------------------------


def test_etsn_result_and_elimination_flow(client):
    client.post(
        "/api/survivor/pick", json={"leg": "1", "member": "vijai", "team": "LAC"}
    )
    r = client.patch("/api/survivor/pick", json={"leg": "1", "etsn": "123456789012"})
    assert r.json()["etsn"] == "123456789012"

    client.post("/api/survivor/result", json={"leg": "1", "result": "win"})
    status = client.get("/api/survivor/status").json()
    assert status["entry"]["alive"] is True
    assert status["entry"]["survived"] == 1

    # A loss (or tie, entered as loss) kills the entry at that leg.
    client.post("/api/survivor/result", json={"leg": "1", "result": "loss"})
    status = client.get("/api/survivor/status").json()
    assert status["entry"]["alive"] is False
    assert status["entry"]["at_leg"] == "1"
    assert "LAC" in status["entry"]["reason"]


def test_result_requires_locked_pick(client):
    assert (
        client.post("/api/survivor/result", json={"leg": "1", "result": "win"}).status_code
        == 404
    )
    assert client.get("/api/survivor/pick", params={"leg": "1"}).status_code == 404


# --- auto-grading from ESPN finals -------------------------------------------


def test_auto_grade_win_and_not_final_skip(client, env, monkeypatch):
    monkeypatch.setattr(
        contest_api,
        "_finals_source",
        lambda: ESPN(
            sport="nfl", transport=scoreboard_transport("espn_nfl_scoreboard_finals_week1")
        ),
    )
    # LAC beat KC in the fixture -> straight-up win for the pick.
    client.post(
        "/api/survivor/pick", json={"leg": "1", "member": "vijai", "team": "LAC"}
    )
    out = client.post("/api/survivor/result/auto", params={"leg": "1"}).json()
    assert out["result"] == "win"
    assert out["pick"]["result"] == "win"


def test_auto_grade_skips_in_progress_game(client, env, monkeypatch):
    monkeypatch.setattr(
        contest_api,
        "_finals_source",
        lambda: ESPN(
            sport="nfl", transport=scoreboard_transport("espn_nfl_scoreboard_finals_week1")
        ),
    )
    client.post(
        "/api/survivor/pick", json={"leg": "1", "member": "vijai", "team": "SEA"}
    )
    out = client.post("/api/survivor/result/auto", params={"leg": "1"}).json()
    assert out["result"] is None
    assert out["skipped_reason"] == "game not final"
    assert out["pick"]["result"] is None
