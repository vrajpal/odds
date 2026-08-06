"""Auto-grading (D-023): grade_pick math, ESPN finals parsing against a real
recorded completed slate, and the end-to-end auto-grade endpoint."""

import json
from datetime import UTC, date, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FIXTURES, make_nfl_spread_odds
from mlb_odds import contest, contest_api
from mlb_odds.contest import grade_pick
from mlb_odds.providers.espn import ESPN
from mlb_odds.storage import Storage

# --- grade_pick: pure ATS math against the static contest number ---


@pytest.mark.parametrize(
    ("side", "home_spread", "home", "away", "expected"),
    [
        # Home favorite -3.5 wins by 7: covers.
        ("home", -3.5, 27, 20, "win"),
        ("away", -3.5, 27, 20, "loss"),
        # Home favorite -7.5 wins by 7: fails to cover.
        ("home", -7.5, 27, 20, "loss"),
        ("away", -7.5, 27, 20, "win"),
        # Lands exactly on the number: push both ways.
        ("home", -7.0, 27, 20, "push"),
        ("away", -7.0, 27, 20, "push"),
        # Home underdog +3.0 loses by 3: push.
        ("home", 3.0, 20, 23, "push"),
        # Home underdog +3.5 loses by 3: covers.
        ("home", 3.5, 20, 23, "win"),
        # Outright home upset as underdog: covers.
        ("home", 6.5, 24, 21, "win"),
        # Pick'em (0) decided by the margin sign.
        ("away", 0.0, 17, 20, "win"),
    ],
)
def test_grade_pick_table(side, home_spread, home, away, expected):
    assert grade_pick(side, home_spread, home, away) == expected


def test_grade_pick_rejects_bad_side():
    with pytest.raises(ValueError):
        grade_pick("over", -3.5, 27, 20)


# --- ESPN finals parsing: real recorded completed slate (2026-08-05 MLB) ---


def scoreboard_transport(fixture: str) -> httpx.MockTransport:
    payload = json.loads((FIXTURES / f"{fixture}.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        assert "dates" in dict(request.url.params)
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def test_fetch_final_scores_parses_real_completed_slate():
    provider = ESPN(sport="mlb", transport=scoreboard_transport("espn_mlb_scoreboard_final"))
    finals = provider.fetch_final_scores(date(2026, 8, 5))
    by_matchup = {(f.away_team, f.home_team): f for f in finals}
    tor = by_matchup[("TOR", "HOU")]
    assert (tor.away_score, tor.home_score, tor.completed) == (5, 4, True)
    lad = by_matchup[("LAD", "CHC")]
    assert (lad.away_score, lad.home_score) == (6, 7)


def test_fetch_final_scores_flags_in_progress_games():
    provider = ESPN(
        sport="nfl", transport=scoreboard_transport("espn_nfl_scoreboard_finals_week1")
    )
    finals = provider.fetch_final_scores(date(2026, 9, 13))
    by_matchup = {(f.away_team, f.home_team): f for f in finals}
    assert by_matchup[("KC", "LAC")].completed is True
    assert by_matchup[("SF", "SEA")].completed is False  # never graded


# --- end-to-end: locked card + lines + finals -> results ---

FROZEN_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)  # Monday after week-1 Sunday
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
            {"circa": -3.0}, FETCH_AT, away=away, home=home, start_time=SUNDAY
        )
        storage.store([go])
        ids[f"{away}@{home}"] = go.game.game_id
    storage.close()
    monkeypatch.setenv("NFL_ODDS_DB", str(nfl_db))
    monkeypatch.setenv("CONTEST_DB", str(tmp_path / "contest.sqlite"))
    monkeypatch.setenv("CONTEST_MEMBERS", "vijai,sam,alex")
    monkeypatch.setattr(contest_api, "_now", lambda: FROZEN_NOW)
    monkeypatch.setattr(
        contest_api,
        "_finals_source",
        lambda: ESPN(
            sport="nfl", transport=scoreboard_transport("espn_nfl_scoreboard_finals_week1")
        ),
    )

    # Lock the card directly at the store layer (FROZEN_NOW is past the
    # deadline — the API rightly refuses to lock this late).
    store = contest.ContestStore(tmp_path / "contest.sqlite")
    store.lock_card(
        1,
        [(gid, "home") for gid in ids.values()],
        locked_by="vijai",
        locked_at=datetime(2026, 9, 12, 20, 0, tzinfo=UTC),
    )
    # Contest lines for all but DAL@NYG (its skip reason under test).
    entered = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
    store.set_line(1, ids["KC@LAC"], -2.5, entered_at=entered)  # LAC won by 7 -> win
    store.set_line(1, ids["BUF@MIA"], 3.0, entered_at=entered)  # MIA lost by 3 -> push
    store.set_line(1, ids["SF@SEA"], -3.0, entered_at=entered)  # in progress
    store.set_line(1, ids["GB@CHI"], -3.0, entered_at=entered)  # no score in fixture
    store.close()
    return ids


@pytest.fixture
def client(env):
    return TestClient(contest_api.app, raise_server_exceptions=False)


def test_auto_grade_end_to_end(client, env):
    out = client.post("/api/contest/results/auto", params={"week": 1}).json()

    assert out["graded"] == {env["KC@LAC"]: "win", env["BUF@MIA"]: "push"}
    reasons = {s["game_id"]: s["reason"] for s in out["skipped"]}
    assert reasons[env["DAL@NYG"]] == "no contest line entered"
    assert reasons[env["SF@SEA"]] == "game not final"
    assert reasons[env["GB@CHI"]] == "no final score found"

    results = {p["game_id"]: p["result"] for p in out["card"]["picks"]}
    assert results[env["KC@LAC"]] == "win"
    assert results[env["SF@SEA"]] is None

    # Reflected in the season rollup immediately.
    season = client.get("/api/contest/season").json()
    assert season["weeks"][0]["wins"] == 1
    assert season["weeks"][0]["pushes"] == 1
    assert season["total_points"] == 1.5


def test_auto_grade_is_idempotent_and_corrects(client, env):
    first = client.post("/api/contest/results/auto", params={"week": 1}).json()
    second = client.post("/api/contest/results/auto", params={"week": 1}).json()
    assert first["graded"] == second["graded"]
    # A manual correction survives only until regraded from the same finals.
    client.post(
        "/api/contest/results",
        json={"week": 1, "results": {env["KC@LAC"]: "loss"}},
    )
    regraded = client.post("/api/contest/results/auto", params={"week": 1}).json()
    by_game = {p["game_id"]: p["result"] for p in regraded["card"]["picks"]}
    assert by_game[env["KC@LAC"]] == "win"  # regrade overwrote the manual "loss"
    assert regraded["graded"][env["KC@LAC"]] == "win"


def test_auto_grade_requires_locked_card(tmp_path, monkeypatch):
    monkeypatch.setenv("NFL_ODDS_DB", str(tmp_path / "nfl.sqlite"))
    monkeypatch.setenv("CONTEST_DB", str(tmp_path / "contest.sqlite"))
    Storage(tmp_path / "nfl.sqlite").close()
    c = TestClient(contest_api.app, raise_server_exceptions=False)
    assert c.post("/api/contest/results/auto", params={"week": 1}).status_code == 404
