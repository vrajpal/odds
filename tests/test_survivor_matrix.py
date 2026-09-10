"""Survivor matrix (D-040): the season look-ahead endpoint and the shared
per-game probability helpers it now shares with the Board.

The fixture season is deliberately richer than test_survivor_api's: enough
lined games (>= 8, the ridge fits' floor) that both market fits succeed, so
the moneyline lens, the spread lens, and their blend are all exercised
rather than silently absent. Every expectation is derived from the same
storage the endpoint reads — the tests pin composition, not magic numbers."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_nfl_spread_odds
from mlb_odds import contest, contest_api, model, survivor, survivor_api, valuation
from mlb_odds.storage import Storage
from mlb_odds.teams import NFL_CODES, NFL_DIVISIONS

FROZEN_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)  # Thu of contest week 1
FETCH_AT = datetime(2026, 8, 1, 17, 0, tzinfo=UTC)
WEEK1 = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)
WEEK2 = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)
WEEK3 = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)
THANKSGIVING = datetime(2026, 11, 26, 21, 30, tzinfo=UTC)

# (away, home, kickoff, {book: home spread}, {book: (away ML, home ML)})
SEASON: list[tuple[str, str, datetime, dict[str, float], dict[str, tuple[int, int]]]] = [
    (
        "KC",
        "LAC",
        WEEK1,
        {"circa": -3.0, "draftkings": -4.0},
        {"circa": (150, -170), "draftkings": (160, -180)},
    ),
    ("SF", "SEA", WEEK1, {"circa": -2.5, "draftkings": -2.5}, {}),  # spread-only
    ("GB", "CHI", WEEK1, {"circa": -1.0}, {"circa": (100, -120)}),
    ("BUF", "NYJ", WEEK1, {"circa": 6.5, "draftkings": 7.0}, {"circa": (-280, 230)}),
    ("LAC", "LV", WEEK2, {"circa": -2.0}, {"circa": (110, -130)}),
    ("KC", "DEN", WEEK2, {"circa": 1.0}, {"circa": (-115, -105)}),
    ("SEA", "SF", WEEK2, {"circa": -7.0, "draftkings": -6.5}, {"circa": (260, -320)}),
    ("CHI", "GB", WEEK2, {"circa": -6.5}, {"circa": (240, -290)}),
    ("LAC", "KC", WEEK3, {"circa": -3.0}, {"circa": (140, -160)}),
    ("BUF", "MIA", WEEK3, {"circa": 3.0}, {"circa": (-160, 140)}),
    ("DET", "DAL", THANKSGIVING, {"circa": -3.0, "draftkings": -3.5}, {"circa": (135, -155)}),
]


def build_season(nfl_db: Path) -> dict[str, str]:
    """Store the fixture season; returns 'AWAY@HOME' -> game_id."""
    storage = Storage(nfl_db)
    ids = {}
    try:
        for away, home, start, lines, moneylines in SEASON:
            go = make_nfl_spread_odds(
                lines,
                FETCH_AT,
                away=away,
                home=home,
                start_time=start,
                moneylines=moneylines or None,
            )
            storage.store([go])
            ids[f"{away}@{home}"] = go.game.game_id
    finally:
        storage.close()
    return ids


@pytest.fixture
def env(tmp_path, monkeypatch):
    nfl_db = tmp_path / "nfl-odds.sqlite"
    ids = build_season(nfl_db)
    monkeypatch.setenv("NFL_ODDS_DB", str(nfl_db))
    monkeypatch.setenv("CONTEST_DB", str(tmp_path / "contest.sqlite"))
    monkeypatch.setenv("CONTEST_MEMBERS", "vijai,sam,alex")
    monkeypatch.setattr(contest_api, "_now", lambda: FROZEN_NOW)
    return {"ids": ids, "nfl_db": nfl_db}


@pytest.fixture
def client(env):
    return TestClient(contest_api.app, raise_server_exceptions=False)


def matrix(client) -> dict:
    r = client.get("/api/survivor/matrix")
    assert r.status_code == 200, r.text
    return r.json()


def teams_of(body: dict) -> dict[str, dict]:
    return {t["team"]: t for t in body["teams"]}


def lock_pick(client, leg: str, team: str) -> None:
    for member in ("vijai", "sam", "alex"):
        r = client.post(
            "/api/survivor/proposal",
            json={"leg": leg, "member": member, "choices": [{"team": team}]},
        )
        assert r.status_code == 201, r.text
    r = client.post("/api/survivor/pick", json={"leg": leg, "member": "vijai", "team": team})
    assert r.status_code == 201, r.text


# --- shape ----------------------------------------------------------------------


def test_shape_every_team_every_leg_every_field(client):
    body = matrix(client)
    assert body["current_leg"] == "1"
    assert [lg["leg_id"] for lg in body["legs"]] == [lg.leg_id for lg in survivor.LEGS]
    teams = teams_of(body)
    assert set(teams) == NFL_CODES
    assert body["teams"] == sorted(body["teams"], key=lambda t: t["team"])
    for team, row in teams.items():
        assert row["division"] == NFL_DIVISIONS[team]
        assert row["used"] is None
        for leg_id, cell in row["cells"].items():
            assert leg_id in survivor.LEG_INDEX
            assert set(cell) == {
                "game_id",
                "opponent",
                "home",
                "start_time",
                "spread",
                "market_win_prob",
                "model_win_prob",
                "divisional",
            }
            assert cell["opponent"] in NFL_CODES and cell["opponent"] != team
            # Pacific ISO, like every other survivor timestamp.
            assert cell["start_time"].endswith(("-07:00", "-08:00"))
            for key in ("market_win_prob", "model_win_prob"):
                assert cell[key] is None or 0.0 <= cell[key] <= 1.0
    scheduled = sum(len(row["cells"]) for row in teams.values())
    assert scheduled == 2 * len(SEASON)  # every stored game appears from both sides


def test_home_and_away_cells_mirror_each_other(client, env):
    teams = teams_of(matrix(client))
    for away, home, _start, _lines, _ml in SEASON:
        game_id = env["ids"][f"{away}@{home}"]
        h = next(c for c in teams[home]["cells"].values() if c["game_id"] == game_id)
        a = next(c for c in teams[away]["cells"].values() if c["game_id"] == game_id)
        assert (h["home"], a["home"]) == (True, False)
        assert (h["opponent"], a["opponent"]) == (away, home)
        assert h["spread"] == -a["spread"]
        assert h["market_win_prob"] + a["market_win_prob"] == pytest.approx(1.0, abs=1e-3)
        assert h["model_win_prob"] + a["model_win_prob"] == pytest.approx(1.0, abs=1e-4)
        assert h["divisional"] == a["divisional"] == (NFL_DIVISIONS[home] == NFL_DIVISIONS[away])


# --- probability composition -------------------------------------------------


def test_market_prob_prefers_devigged_moneyline_over_spread(client, env):
    teams = teams_of(matrix(client))
    storage = Storage(env["nfl_db"], read_only=True)
    try:
        kc_lac = env["ids"]["KC@LAC"]
        expected_ml = valuation.consensus_prob(
            valuation.book_probs(valuation.moneyline_history(storage, kc_lac))
        )
    finally:
        storage.close()
    assert expected_ml is not None
    lac = teams["LAC"]["cells"]["1"]
    assert lac["market_win_prob"] == expected_ml
    assert lac["market_win_prob"] != survivor.win_probability(-3.5)  # not the spread path
    assert teams["KC"]["cells"]["1"]["market_win_prob"] == round(1 - expected_ml, 3)

    # No moneyline stored -> spread-implied conversion of the median spread.
    sea = teams["SEA"]["cells"]["1"]
    assert sea["spread"] == -2.5
    assert sea["market_win_prob"] == survivor.win_probability(-2.5)
    assert teams["SF"]["cells"]["1"]["market_win_prob"] == round(
        1 - survivor.win_probability(-2.5), 3
    )


def test_model_prob_is_the_shared_two_lens_blend(client, env):
    """With >= 8 lined games both fits succeed; the matrix number must be
    exactly nfl_model_prob(moneyline lens, spread lens) on those fits."""
    teams = teams_of(matrix(client))
    storage = Storage(env["nfl_db"], read_only=True)
    try:
        ratings, hfa = contest.power_ratings(storage)
        strengths, ml_hfa = valuation.implied_strengths(storage)
    finally:
        storage.close()
    for away, home, _start, _lines, _ml in SEASON:
        game_id = env["ids"][f"{away}@{home}"]
        ml_lens = valuation.model_home_prob(strengths, ml_hfa, home, away)
        line = contest.predicted_home_spread(ratings, hfa, home, away)
        expected, spread_lens = model.nfl_model_prob(ml_lens, line)
        assert expected is not None and ml_lens is not None and spread_lens is not None
        h = next(c for c in teams[home]["cells"].values() if c["game_id"] == game_id)
        a = next(c for c in teams[away]["cells"].values() if c["game_id"] == game_id)
        assert h["model_win_prob"] == expected
        assert a["model_win_prob"] == round(1 - expected, 4)


def test_board_exposes_both_lenses_after_the_refactor(client, env):
    """The Board's per-game math moved into shared helpers; its model fields
    must still be populated and internally consistent."""
    body = client.get("/api/survivor/board", params={"leg": "1"}).json()
    assert len(body["games"]) == 4
    for g in body["games"]:
        assert g["predicted_line"] is not None
        assert g["ml_lens_prob"] is not None
        assert g["spread_lens_prob"] == model.margin_to_prob(-g["predicted_line"])
        assert (
            g["model_win_prob"] == model.nfl_model_prob(g["ml_lens_prob"], g["predicted_line"])[0]
        )
        assert g["away_win_prob"] == round(1 - g["home_win_prob"], 3)


def test_matrix_and_board_agree_on_every_leg(client):
    """The two views read the same helpers; this pins that they can never
    drift, leg by leg, number by number."""
    teams = teams_of(matrix(client))
    checked = 0
    for leg_ in survivor.LEGS:
        board = client.get("/api/survivor/board", params={"leg": leg_.leg_id}).json()
        for g in board["games"]:
            h = teams[g["home_team"]]["cells"][leg_.leg_id]
            a = teams[g["away_team"]]["cells"][leg_.leg_id]
            assert h["game_id"] == a["game_id"] == g["game_id"]
            assert h["spread"] == g["consensus"]
            assert h["market_win_prob"] == g["home_win_prob"]
            assert a["market_win_prob"] == g["away_win_prob"]
            assert h["model_win_prob"] == g["model_win_prob"]
            assert h["start_time"] == g["start_time"]
            assert h["divisional"] == g["divisional"]
            checked += 1
    assert checked == len(SEASON)


# --- schedule mapping --------------------------------------------------------


def test_leg_containing_is_half_open_on_leg_boundaries():
    week2 = survivor.LEGS[1]
    assert survivor_api._leg_containing(week2.start).leg_id == "2"
    assert survivor_api._leg_containing(week2.start - timedelta(seconds=1)).leg_id == "1"
    # The calendar has a one-day seam between weeks 12 and 13 (D-028).
    week12, week13 = survivor.leg("12"), survivor.leg("13")
    assert week12.end < week13.start
    assert survivor_api._leg_containing(week12.end) is None
    assert survivor_api._leg_containing(survivor.LEGS[-1].end) is None


def test_games_in_calendar_seams_belong_to_no_leg(client, env):
    """A game that lands in the seam between two legs is not pickable in
    either, so the matrix must not show it anywhere."""
    seam = survivor.leg("12").end + timedelta(hours=12)
    storage = Storage(env["nfl_db"])
    try:
        go = make_nfl_spread_odds(
            {"circa": -1.0}, FETCH_AT, away="TEN", home="JAX", start_time=seam
        )
        storage.store([go])
    finally:
        storage.close()
    teams = teams_of(matrix(client))
    assert teams["TEN"]["cells"] == {} and teams["JAX"]["cells"] == {}


def test_thanksgiving_game_lands_in_the_holiday_leg_not_week_12(client):
    teams = teams_of(matrix(client))
    assert set(teams["DET"]["cells"]) == {"TG"}
    assert set(teams["DAL"]["cells"]) == {"TG"}
    assert teams["DET"]["cells"]["TG"]["opponent"] == "DAL"


def test_two_games_in_one_leg_keep_the_earliest_kickoff(client, env):
    """Mirrors pick validation (_game_for_team takes the earliest game), so
    the matrix cell is the game a pick would actually be validated against."""
    later = WEEK2 + timedelta(days=1, hours=3)  # Monday night
    storage = Storage(env["nfl_db"])
    try:
        go = make_nfl_spread_odds(
            {"circa": -4.0}, FETCH_AT, away="LAC", home="SF", start_time=later
        )
        storage.store([go])
    finally:
        storage.close()
    cell = teams_of(matrix(client))["LAC"]["cells"]["2"]
    assert cell["game_id"] == env["ids"]["LAC@LV"]
    assert cell["game_id"] == survivor_api._game_for_team(survivor.leg("2"), "LAC").game_id


def test_byes_are_absent_not_null(client):
    kc = teams_of(matrix(client))["KC"]["cells"]
    assert set(kc) == {"1", "2", "3"}
    assert None not in kc.values()


# --- season state --------------------------------------------------------------


def test_locked_pick_used_team_and_graded_result(client, monkeypatch):
    lock_pick(client, "1", "LAC")
    body = matrix(client)
    leg1 = body["legs"][0]
    assert (leg1["pick"], leg1["result"], leg1["locked"]) == ("LAC", None, False)
    teams = teams_of(body)
    assert teams["LAC"]["used"] == "1"
    assert set(teams["LAC"]["cells"]) == {"1", "2", "3"}  # schedule stays visible

    r = client.post("/api/survivor/result", json={"leg": "1", "result": "win"})
    assert r.status_code == 200
    # Past the leg-1 deadline but still inside its window: locked, still current.
    monkeypatch.setattr(
        contest_api, "_now", lambda: survivor.leg("1").deadline + timedelta(hours=1)
    )
    body = matrix(client)
    assert body["current_leg"] == "1"
    assert (body["legs"][0]["locked"], body["legs"][0]["result"]) == (True, "win")
    assert body["legs"][1]["locked"] is False
    # Into week 2: the current leg advances, leg 1 stays locked.
    monkeypatch.setattr(contest_api, "_now", lambda: survivor.leg("2").start + timedelta(days=1))
    body = matrix(client)
    assert body["current_leg"] == "2"
    assert [lg["locked"] for lg in body["legs"][:3]] == [True, False, False]


def test_season_over_has_no_current_leg(client, monkeypatch):
    monkeypatch.setattr(contest_api, "_now", lambda: survivor.LEGS[-1].end + timedelta(days=1))
    body = matrix(client)
    assert body["current_leg"] is None
    assert all(lg["locked"] for lg in body["legs"])


# --- degraded inputs -----------------------------------------------------------


def test_missing_nfl_db_is_503(client, tmp_path, monkeypatch):
    monkeypatch.setenv("NFL_ODDS_DB", str(tmp_path / "nope.sqlite"))
    r = client.get("/api/survivor/matrix")
    assert r.status_code == 503
    assert not (tmp_path / "nope.sqlite").exists()  # read-only by design (D-012)


def test_empty_schedule_is_all_byes(tmp_path, monkeypatch):
    empty = tmp_path / "empty.sqlite"
    Storage(empty).close()
    monkeypatch.setenv("NFL_ODDS_DB", str(empty))
    monkeypatch.setenv("CONTEST_DB", str(tmp_path / "contest.sqlite"))
    monkeypatch.setattr(contest_api, "_now", lambda: FROZEN_NOW)
    body = matrix(TestClient(contest_api.app, raise_server_exceptions=False))
    assert len(body["legs"]) == 20
    assert len(body["teams"]) == 32
    assert all(t["cells"] == {} for t in body["teams"])


def test_too_few_lined_games_leaves_model_lens_absent(tmp_path, monkeypatch):
    """Below the fits' floor the model number must degrade to the spread
    lens alone (or nothing), never to a fake 0.5."""
    nfl_db = tmp_path / "thin.sqlite"
    storage = Storage(nfl_db)
    try:
        storage.store([make_nfl_spread_odds({"circa": -3.0}, FETCH_AT, start_time=WEEK1)])
    finally:
        storage.close()
    monkeypatch.setenv("NFL_ODDS_DB", str(nfl_db))
    monkeypatch.setenv("CONTEST_DB", str(tmp_path / "contest.sqlite"))
    monkeypatch.setattr(contest_api, "_now", lambda: FROZEN_NOW)
    teams = teams_of(matrix(TestClient(contest_api.app, raise_server_exceptions=False)))
    cell = teams["LAC"]["cells"]["1"]
    assert cell["market_win_prob"] == survivor.win_probability(-3.0)
    assert cell["model_win_prob"] is None
    assert teams["KC"]["cells"]["1"]["model_win_prob"] is None
