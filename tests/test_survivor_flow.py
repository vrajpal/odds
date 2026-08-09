"""API-level integration of the survivor season: the clock moves, picks lock
leg after leg, constraints compound, and the holiday traps arm and fire.

Unlike test_survivor_api.py (single-endpoint behavior at one frozen instant),
these tests walk multi-leg scenarios through a mutable clock — the closest the
suite gets to a season actually happening. ESPN finals are served from
synthetic scoreboard payloads built per test, so multi-day legs can assert
which scoreboard *date* auto-grading asked for."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import make_nfl_spread_odds
from mlb_odds import contest_api, survivor
from mlb_odds.providers.espn import ESPN
from mlb_odds.storage import Storage
from mlb_odds.teams import _NFL_FULL_NAMES

FULL_NAME = {code: name for name, code in _NFL_FULL_NAMES.items()}

FETCH_AT = datetime(2026, 8, 1, 17, 0, tzinfo=UTC)
WEEK1_THURSDAY = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
WEEK2_THURSDAY = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

# The seeded slate spans four legs so a season can actually progress:
# week 1 and week 2 Sundays, a week-11 Sunday (PIT — the last Thanksgiving
# slate team in the endgame test), and two Thanksgiving-leg games on
# *different scoreboard days* (Thursday and Black Friday).
MATCHUPS = [
    ("KC", "LAC", datetime(2026, 9, 13, 17, 0, tzinfo=UTC)),
    ("SF", "SEA", datetime(2026, 9, 13, 17, 0, tzinfo=UTC)),
    ("DAL", "NYG", datetime(2026, 9, 20, 17, 0, tzinfo=UTC)),
    ("LAC", "LV", datetime(2026, 9, 20, 17, 0, tzinfo=UTC)),  # LAC's week-2 game
    ("PIT", "BAL", datetime(2026, 11, 22, 18, 0, tzinfo=UTC)),
    ("CHI", "DET", datetime(2026, 11, 26, 18, 0, tzinfo=UTC)),  # Thanksgiving Thu
    ("DEN", "PIT", datetime(2026, 11, 27, 20, 0, tzinfo=UTC)),  # Black Friday
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    nfl_db = tmp_path / "nfl-odds.sqlite"
    storage = Storage(nfl_db)
    ids = {}
    for away, home, start in MATCHUPS:
        go = make_nfl_spread_odds(
            {"circa": -3.0, "draftkings": -2.5}, FETCH_AT, away=away, home=home,
            start_time=start,
        )
        storage.store([go])
        ids[f"{away}@{home}"] = go.game.game_id
    storage.close()
    monkeypatch.setenv("NFL_ODDS_DB", str(nfl_db))
    monkeypatch.setenv("CONTEST_DB", str(tmp_path / "contest.sqlite"))
    monkeypatch.setenv("CONTEST_MEMBERS", "vijai,sam,alex")
    monkeypatch.delenv("CONTEST_MEMBER_EMAILS", raising=False)
    return {"ids": ids, "contest_db": tmp_path / "contest.sqlite"}


@pytest.fixture
def clock(monkeypatch):
    """Mutable now: tests advance the season by assigning holder['now']."""
    holder = {"now": WEEK1_THURSDAY}
    monkeypatch.setattr(contest_api, "_now", lambda: holder["now"])
    return holder


@pytest.fixture
def client(env, clock):
    return TestClient(contest_api.app, raise_server_exceptions=False)


# --- synthetic ESPN scoreboards ----------------------------------------------


def scoreboard(*finals: tuple[str, str, int, int, bool]) -> dict:
    """A minimal-but-real scoreboard payload: (away, home, away_pts, home_pts,
    completed) tuples using canonical codes, expanded to ESPN displayNames."""
    return {
        "events": [
            {
                "id": str(i),
                "date": "2026-01-01T00:00Z",  # unused by grading; must parse
                "status": {"type": {"completed": completed, "name": "STATUS_FINAL"}},
                "competitions": [
                    {
                        "competitors": [
                            {
                                "homeAway": "home",
                                "team": {"displayName": FULL_NAME[home]},
                                "score": str(home_pts),
                            },
                            {
                                "homeAway": "away",
                                "team": {"displayName": FULL_NAME[away]},
                                "score": str(away_pts),
                            },
                        ]
                    }
                ],
            }
            for i, (away, home, away_pts, home_pts, completed) in enumerate(finals)
        ]
    }


def finals_source(monkeypatch, payload: dict) -> list[str]:
    """Point auto-grading at a canned scoreboard; returns the list of `dates`
    params ESPN was asked for, so tests can assert the ET-day math."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(dict(request.url.params)["dates"])
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        contest_api,
        "_finals_source",
        lambda: ESPN(sport="nfl", transport=httpx.MockTransport(handler)),
    )
    return requested


# --- the season, leg by leg --------------------------------------------------


def test_two_leg_season_walk(client, env, clock, monkeypatch):
    """Week 1 propose->reveal->vote->lock->grade, clock forward, week 2 with
    the used-team constraint biting — state accumulating across legs."""
    # -- week 1: blind phase (minutes apart, like a real Thursday) --
    for member, team in (("vijai", "LAC"), ("sam", "LAC")):
        assert (
            client.post(
                "/api/survivor/proposal",
                json={"leg": "1", "member": member, "choices": [{"team": team}]},
            ).status_code
            == 201
        )
        clock["now"] += timedelta(minutes=5)
    # alex hasn't submitted: sees nobody's teams, reveal refused.
    view = client.get(
        "/api/survivor/proposals", params={"leg": "1", "member": "alex"}
    ).json()
    assert view["proposals"] == [] and view["submitted"] == ["vijai", "sam"]
    assert (
        client.get(
            "/api/survivor/consensus", params={"leg": "1", "member": "alex"}
        ).status_code
        == 409
    )

    client.post(
        "/api/survivor/proposal",
        json={"leg": "1", "member": "alex", "choices": [{"team": "KC"}]},
    )
    c = client.get(
        "/api/survivor/consensus", params={"leg": "1", "member": "alex"}
    ).json()
    assert c["working_pick"]["team"] == "LAC"
    assert c["working_pick"]["status"] == "majority"
    assert c["working_pick_warnings"] == []  # LAC is on neither holiday slate

    # alex concedes; unanimity; the captain locks.
    c = client.post(
        "/api/survivor/vote", json={"leg": "1", "member": "alex", "team": "LAC"}
    ).json()
    assert c["working_pick"]["status"] == "unanimous"
    assert (
        client.post(
            "/api/survivor/pick", json={"leg": "1", "member": "vijai", "team": "LAC"}
        ).status_code
        == 201
    )
    client.patch("/api/survivor/pick", json={"leg": "1", "etsn": "123456789012"})
    client.post("/api/survivor/result", json={"leg": "1", "result": "win"})

    status = client.get("/api/survivor/status").json()
    assert status["entry"] == {
        "alive": True, "survived": 1, "reason": None, "at_leg": None,
    }
    assert status["used"] == {"LAC": "1"}

    # -- clock forward into week 2 --
    clock["now"] = WEEK2_THURSDAY
    board = client.get("/api/survivor/board").json()
    assert board["leg_id"] == "2"
    assert board["captain"] == "sam"  # rotation moved with the leg
    games = {g["game_id"]: g for g in board["games"]}
    assert set(games) == {env["ids"]["DAL@NYG"], env["ids"]["LAC@LV"]}
    # The burned team is flagged on its new game's row.
    assert games[env["ids"]["LAC@LV"]]["away_used"] == "1"

    # Last week's team is burned: proposals and locks refuse it even though
    # it plays this leg.
    assert (
        client.post(
            "/api/survivor/proposal",
            json={"leg": "2", "member": "vijai", "choices": [{"team": "LAC"}]},
        ).status_code
        == 409
    )
    client.post(
        "/api/survivor/pick", json={"leg": "2", "member": "sam", "team": "DAL"}
    )
    client.post("/api/survivor/result", json={"leg": "2", "result": "win"})

    status = client.get("/api/survivor/status").json()
    assert status["entry"]["survived"] == 2
    assert status["used"] == {"LAC": "1", "DAL": "2"}
    legs = {lg["leg_id"]: lg for lg in status["legs"]}
    assert legs["1"]["pick"]["etsn"] == "123456789012"
    assert legs["2"]["pick"]["result"] == "win"


def test_missed_deadline_eliminates_as_time_passes(client, clock):
    """The same empty state flips from alive to eliminated purely because the
    clock crossed the week-1 deadline — and locking is refused after."""
    assert client.get("/api/survivor/status").json()["entry"]["alive"] is True

    clock["now"] = survivor.leg("1").deadline.astimezone(UTC)
    status = client.get("/api/survivor/status").json()
    assert status["entry"]["alive"] is False
    assert status["entry"]["at_leg"] == "1"
    assert "deadline" in status["entry"]["reason"]

    r = client.post(
        "/api/survivor/pick", json={"leg": "1", "member": "vijai", "team": "LAC"}
    )
    assert r.status_code == 409 and "deadline" in r.json()["detail"]


def test_holiday_endgame_critical_then_fatal(client, env, clock):
    """Ten legs of picks burn nine Thanksgiving teams; the eleventh lock takes
    the last one. The outlook escalates critical -> fatal and the fatal lock
    warning fires — the exact Rule-8 trap the Plan tab exists to prevent."""
    store = survivor.SurvivorStore(env["contest_db"])
    tg_nine = ["GB", "LAR", "CHI", "DET", "PHI", "DAL", "KC", "BUF", "DEN"]
    for i, team in enumerate(tg_nine + ["SF"], start=1):  # SF keeps leg 10 alive
        store.lock_pick(
            str(i), team, f"seeded-{i}", locked_by="vijai", locked_at=clock["now"]
        )
    store.close()

    clock["now"] = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)  # inside week 11
    status = client.get("/api/survivor/status").json()
    assert status["entry"]["alive"] is True
    outlook = {o["leg_id"]: o for o in status["holiday_outlook"]}
    assert outlook["TG"] == {
        "leg_id": "TG", "label": "Thanksgiving", "picked": False,
        "remaining": ["PIT"], "danger": "critical",
    }

    # Locking PIT for week 11 is legal — and announced as suicide.
    r = client.post(
        "/api/survivor/pick", json={"leg": "11", "member": "vijai", "team": "PIT"}
    )
    assert r.status_code == 201
    (fatal,) = [w for w in r.json()["warnings"] if w["severity"] == "fatal"]
    assert "guarantees elimination" in fatal["message"]

    outlook = {
        o["leg_id"]: o
        for o in client.get("/api/survivor/status").json()["holiday_outlook"]
    }
    assert outlook["TG"]["danger"] == "fatal" and outlook["TG"]["remaining"] == []
    # The overlap teams are gone too, so Christmas is down to its last two.
    assert outlook["XMAS"]["danger"] == "critical"
    assert outlook["XMAS"]["remaining"] == ["HOU", "SEA"]


# --- multi-day legs and straight-up grading ----------------------------------


def test_thanksgiving_leg_grades_from_the_right_scoreboard_day(
    client, env, clock, monkeypatch
):
    """The TG leg spans three ET days; auto-grading must ask ESPN for the
    picked game's own day (Thanksgiving Thursday here), not the leg's start."""
    requested = finals_source(
        monkeypatch, scoreboard(("CHI", "DET", 24, 17, True))
    )
    clock["now"] = datetime(2026, 11, 24, 18, 0, tzinfo=UTC)  # TG window open
    board = client.get("/api/survivor/board", params={"leg": "TG"}).json()
    assert {g["game_id"] for g in board["games"]} == {
        env["ids"]["CHI@DET"], env["ids"]["DEN@PIT"]
    }

    assert (
        client.post(
            "/api/survivor/pick", json={"leg": "TG", "member": "vijai", "team": "CHI"}
        ).status_code
        == 201
    )
    out = client.post("/api/survivor/result/auto", params={"leg": "TG"}).json()
    assert out["result"] == "win"  # CHI won outright on the road
    assert requested == ["20261126"]  # Thanksgiving Thursday, ET


def test_autograded_tie_is_a_loss_and_kills_the_entry(client, env, clock, monkeypatch):
    """Rule 6a end to end: a 20-20 final grades the pick as a loss and the
    status endpoint reports the entry eliminated at that leg."""
    finals_source(monkeypatch, scoreboard(("SF", "SEA", 20, 20, True)))
    client.post(
        "/api/survivor/pick", json={"leg": "1", "member": "vijai", "team": "SEA"}
    )
    out = client.post("/api/survivor/result/auto", params={"leg": "1"}).json()
    assert out["result"] == "loss"

    entry = client.get("/api/survivor/status").json()["entry"]
    assert entry["alive"] is False
    assert entry["at_leg"] == "1"
    assert "SEA" in entry["reason"] and "ties" in entry["reason"]


def test_autograde_correction_overwrites(client, env, clock, monkeypatch):
    """A corrected final (score change on ESPN's side) regrades cleanly —
    grading is idempotent overwrite, mirroring the Million behavior."""
    finals_source(monkeypatch, scoreboard(("KC", "LAC", 27, 20, True)))
    client.post(
        "/api/survivor/pick", json={"leg": "1", "member": "vijai", "team": "LAC"}
    )
    assert (
        client.post("/api/survivor/result/auto", params={"leg": "1"}).json()["result"]
        == "loss"
    )
    finals_source(monkeypatch, scoreboard(("KC", "LAC", 20, 27, True)))
    assert (
        client.post("/api/survivor/result/auto", params={"leg": "1"}).json()["result"]
        == "win"
    )
    assert client.get("/api/survivor/status").json()["entry"]["alive"] is True


# --- Cloudflare Access identity on survivor acting endpoints -----------------


class TestSurvivorIdentity:
    @pytest.fixture
    def public_env(self, env, monkeypatch):
        monkeypatch.setenv(
            "CONTEST_MEMBER_EMAILS", "vijai@example.com:vijai,sam@example.com:sam"
        )
        return env

    @pytest.fixture
    def client(self, public_env, clock):
        return TestClient(contest_api.app, raise_server_exceptions=False)

    HEADER = {"Cf-Access-Authenticated-User-Email": "vijai@example.com"}

    def test_public_user_cannot_act_as_someone_else(self, client):
        for path, body in (
            ("/api/survivor/proposal", {"leg": "1", "member": "sam", "choices": [{"team": "LAC"}]}),
            ("/api/survivor/vote", {"leg": "1", "member": "sam", "team": "LAC"}),
            ("/api/survivor/pick", {"leg": "1", "member": "sam", "team": "LAC"}),
        ):
            r = client.post(path, json=body, headers=self.HEADER)
            assert r.status_code == 403, path
            assert "cannot act as sam" in r.json()["detail"]

    def test_mapped_user_acts_as_self(self, client):
        r = client.post(
            "/api/survivor/proposal",
            json={"leg": "1", "member": "vijai", "choices": [{"team": "LAC"}]},
            headers=self.HEADER,
        )
        assert r.status_code == 201

    def test_unmapped_access_email_403(self, client):
        r = client.post(
            "/api/survivor/proposal",
            json={"leg": "1", "member": "vijai", "choices": [{"team": "LAC"}]},
            headers={"Cf-Access-Authenticated-User-Email": "stranger@example.com"},
        )
        assert r.status_code == 403
        assert "not mapped" in r.json()["detail"]

    def test_tailnet_path_stays_honor_system(self, client):
        r = client.post(
            "/api/survivor/proposal",
            json={"leg": "1", "member": "sam", "choices": [{"team": "KC"}]},
        )
        assert r.status_code == 201
