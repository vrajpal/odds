"""API-level test of the full weekly flow: blind propose → reveal → vote →
lock (Rule 8 enforced) → ETSN → grade → season. Time goes through
contest_api._now, monkeypatched to a fixed Friday inside week 1 so the suite
is deterministic forever."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from conftest import make_nfl_spread_odds
from mlb_odds import contest_api
from mlb_odds.storage import Storage

MEMBERS = "vijai,sam,alex"
FROZEN_NOW = datetime(2026, 9, 11, 19, 0, tzinfo=UTC)  # Fri noon PT, week 1
FETCH_AT = datetime(2026, 8, 1, 17, 0, tzinfo=UTC)

# Five Sunday games plus one Thursday-night game (early kickoff, Rule 8 bait).
SUNDAY = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)
THURSDAY = datetime(2026, 9, 11, 0, 15, tzinfo=UTC)  # Thu 5:15 PM PT
MATCHUPS = [
    ("KC", "LAC", SUNDAY),
    ("BUF", "MIA", SUNDAY),
    ("DAL", "NYG", SUNDAY),
    ("SF", "SEA", SUNDAY),
    ("GB", "CHI", SUNDAY),
    ("DET", "PHI", THURSDAY),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    nfl_db = tmp_path / "nfl-odds.sqlite"
    storage = Storage(nfl_db)
    game_ids = {}
    for away, home, start in MATCHUPS:
        go = make_nfl_spread_odds(
            {"circa": -3.0, "draftkings": -2.5}, FETCH_AT, away=away, home=home,
            start_time=start,
        )
        storage.store([go])
        game_ids[f"{away}@{home}"] = go.game.game_id
    storage.close()
    monkeypatch.setenv("NFL_ODDS_DB", str(nfl_db))
    monkeypatch.setenv("CONTEST_DB", str(tmp_path / "contest.sqlite"))
    monkeypatch.setenv("CONTEST_MEMBERS", MEMBERS)
    monkeypatch.setattr(contest_api, "_now", lambda: FROZEN_NOW)
    return game_ids


@pytest.fixture
def client(env):
    return TestClient(contest_api.app, raise_server_exceptions=False)


def sunday_ids(env):
    return [env[k] for k in ("KC@LAC", "BUF@MIA", "DAL@NYG", "SF@SEA", "GB@CHI")]


def propose(client, member, picks, week=1):
    return client.post(
        "/api/contest/proposals",
        json={"week": week, "member": member,
              "picks": [{"game_id": g, "side": s} for g, s in picks]},
    )


def test_members_and_captains(client):
    body = client.get("/api/contest/members").json()
    assert body["members"] == ["vijai", "sam", "alex"]
    assert body["captains"]["1"] == "vijai"
    assert body["captains"]["2"] == "sam"


def test_board_carries_captain_and_early_kickoff_flags(client, env):
    body = client.get("/api/contest/board", params={"week": 1}).json()
    assert body["captain"] == "vijai"
    assert body["card_locked"] is False
    early = {g["game_id"]: g["early_kickoff"] for g in body["games"]}
    assert early[env["DET@PHI"]] is True
    assert early[env["KC@LAC"]] is False


def test_blind_phase_hides_others_until_submitted(client, env):
    gid = env["KC@LAC"]
    assert propose(client, "vijai", [(gid, "home")]).status_code == 201

    # sam hasn't submitted: consensus is walled off, proposals show only sam's (none).
    walled = client.get(
        "/api/contest/consensus", params={"week": 1, "member": "sam"}
    )
    assert walled.status_code == 409
    view = client.get(
        "/api/contest/proposals", params={"week": 1, "member": "sam"}
    ).json()
    assert view["proposals"] == []
    assert view["submitted"] == ["vijai"]
    assert set(view["waiting_on"]) == {"sam", "alex"}

    # vijai (submitted) sees own picks.
    mine = client.get(
        "/api/contest/proposals", params={"week": 1, "member": "vijai"}
    ).json()
    assert [p["game_id"] for p in mine["proposals"]] == [gid]


def test_resubmission_is_blocked(client, env):
    gid = env["KC@LAC"]
    propose(client, "vijai", [(gid, "home")])
    again = propose(client, "vijai", [(gid, "away")])
    assert again.status_code == 409


def test_unknown_member_403_unknown_game_404(client, env):
    assert propose(client, "stranger", [(env["KC@LAC"], "home")]).status_code == 403
    assert propose(client, "vijai", [("2026-09-13-XX-YY-1", "home")]).status_code == 404


def test_full_flow_to_locked_graded_card(client, env):
    ids = sunday_ids(env)
    # Blind proposals: overlap on three games, disagreement on the rest.
    propose(client, "vijai", [(ids[0], "home"), (ids[1], "home"), (ids[2], "home"),
                              (ids[3], "home"), (ids[4], "home")])
    propose(client, "sam", [(ids[0], "home"), (ids[1], "home"), (ids[2], "home"),
                            (ids[3], "away")])
    propose(client, "alex", [(ids[0], "home"), (ids[1], "home"), (ids[2], "home"),
                             (ids[4], "home")])

    consensus = client.get(
        "/api/contest/consensus", params={"week": 1, "member": "vijai"}
    ).json()
    by_key = {(c["game_id"], c["side"]): c["status"] for c in consensus["candidates"]}
    assert by_key[(ids[0], "home")] == "unanimous"
    assert by_key[(ids[4], "home")] == "majority"  # vijai + alex
    assert by_key[(ids[3], "home")] == "contested"
    assert by_key[(ids[3], "away")] == "contested"
    # No early game in the working card → Saturday deadline, nothing pulls it.
    assert consensus["deadline_pulled_forward_by"] is None

    # sam flips to home on ids[3]: now majority.
    voted = client.post(
        "/api/contest/votes",
        json={"week": 1, "member": "sam", "game_id": ids[3], "side": "home"},
    ).json()
    assert {(c["game_id"], c["side"]): c["status"] for c in voted["candidates"]}[
        (ids[3], "home")
    ] == "majority"

    # Captain locks the top five.
    lock = client.post(
        "/api/contest/card",
        json={"week": 1, "member": "vijai",
              "picks": [{"game_id": g, "side": "home"} for g in ids]},
    )
    assert lock.status_code == 201
    assert lock.json()["effective_deadline"].startswith("2026-09-12T16:00")

    # Voting after lock is refused; so is a second card.
    assert client.post(
        "/api/contest/votes",
        json={"week": 1, "member": "alex", "game_id": ids[0], "side": "away"},
    ).status_code == 409
    assert client.post(
        "/api/contest/card",
        json={"week": 1, "member": "sam",
              "picks": [{"game_id": g, "side": "away"} for g in ids]},
    ).status_code == 409

    # ETSN, grading, season rollup.
    assert client.patch(
        "/api/contest/card", json={"week": 1, "etsn": "123456789012"}
    ).json()["etsn"] == "123456789012"
    graded = client.post(
        "/api/contest/results",
        json={"week": 1, "results": {ids[0]: "win", ids[1]: "win", ids[2]: "win",
                                     ids[3]: "push", ids[4]: "loss"}},
    )
    assert graded.status_code == 200

    season = client.get("/api/contest/season").json()
    assert season["total_points"] == 3.5
    assert season["weeks"][0]["wins"] == 3
    assert season["tiebreakers"]["winning_weeks"] == 1
    assert season["quarters"]["1"] == 3.5
    board = client.get("/api/contest/board", params={"week": 1}).json()
    assert board["card_locked"] is True


def test_rule8_early_pick_pulls_deadline_and_blocks_late_lock(client, env):
    ids = sunday_ids(env)
    thursday_game = env["DET@PHI"]
    for m in ("vijai", "sam", "alex"):
        propose(client, m, [(thursday_game, "home")])
    consensus = client.get(
        "/api/contest/consensus", params={"week": 1, "member": "vijai"}
    ).json()
    # Thursday 5:15 PM PT kickoff pulls the working card's deadline forward.
    assert consensus["deadline_pulled_forward_by"] == thursday_game
    assert consensus["effective_deadline"].startswith("2026-09-10T17:15")

    # Frozen now is Friday — past that kickoff — so locking a card containing
    # the Thursday game must be refused under Rule 8...
    late = client.post(
        "/api/contest/card",
        json={"week": 1, "member": "vijai",
              "picks": [{"game_id": g, "side": "home"} for g in ids[:4] + [thursday_game]]},
    )
    assert late.status_code == 409
    assert "Rule 8" in late.json()["detail"]

    # ...while an all-Sunday card still locks fine at the same instant.
    ok = client.post(
        "/api/contest/card",
        json={"week": 1, "member": "vijai",
              "picks": [{"game_id": g, "side": "home"} for g in ids]},
    )
    assert ok.status_code == 201


def test_results_require_card_and_membership_of_pick(client, env):
    assert client.post(
        "/api/contest/results", json={"week": 1, "results": {env["KC@LAC"]: "win"}}
    ).status_code == 404


def test_booby_eligibility_false_after_missed_completed_week(client, env, monkeypatch):
    # Move the clock to week 3 with no cards ever locked: weeks 1-2 are
    # completed misses → permanently booby-ineligible.
    week3 = datetime(2026, 9, 24, 19, 0, tzinfo=UTC)
    monkeypatch.setattr(contest_api, "_now", lambda: week3)
    season = client.get("/api/contest/season").json()
    assert season["booby_eligible"] is False
    assert season["weeks"] == []


def test_ui_served_at_root(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Circa Million" in resp.text


class TestAccessIdentity:
    """Cloudflare Access identity mapping (D-026): the header locks a public
    user to their member; tailnet requests (no header) are unchanged."""

    HEADERS = {"Cf-Access-Authenticated-User-Email": "Neem@Example.com"}

    @pytest.fixture(autouse=True)
    def emails(self, monkeypatch, env):
        monkeypatch.setenv(
            "CONTEST_MEMBER_EMAILS",
            "vijai@example.com:vijai,neem@example.com:sam,mike@example.com:alex",
        )

    def test_whoami_maps_case_insensitively(self, client):
        who = client.get("/api/contest/whoami", headers=self.HEADERS).json()
        assert who == {"email": "Neem@Example.com", "member": "sam"}

    def test_whoami_null_on_tailnet_path(self, client):
        assert client.get("/api/contest/whoami").json() == {"email": None, "member": None}

    def test_public_user_cannot_act_as_someone_else(self, client, env):
        gid = env["KC@LAC"]
        forged = propose_as(client, "vijai", gid, headers=self.HEADERS)
        assert forged.status_code == 403
        assert "authenticated as sam" in forged.json()["detail"]
        allowed = propose_as(client, "sam", gid, headers=self.HEADERS)
        assert allowed.status_code == 201

    def test_unmapped_access_email_403(self, client, env):
        bad = {"Cf-Access-Authenticated-User-Email": "stranger@example.com"}
        resp = propose_as(client, "vijai", env["KC@LAC"], headers=bad)
        assert resp.status_code == 403
        assert "not mapped" in resp.json()["detail"]

    def test_tailnet_path_still_honor_system(self, client, env):
        assert propose_as(client, "vijai", env["KC@LAC"]).status_code == 201


def propose_as(client, member, game_id, headers=None):
    return client.post(
        "/api/contest/proposals",
        json={"week": 1, "member": member, "picks": [{"game_id": game_id, "side": "home"}]},
        headers=headers or {},
    )
