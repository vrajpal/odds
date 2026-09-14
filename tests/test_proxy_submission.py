"""Proxy submission (D-043): picks reach Circa through a proxy, so the app
records a card or survivor pick after its deadline with `late: true`, the
submission time is tracked separately from the recording time, and the
stats anchor on the submission. ETSN is an optional confirmation."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import make_nfl_spread_odds
from mlb_odds import contest, contest_api, survivor
from mlb_odds.storage import Storage
from test_contest_flow import FETCH_AT, MATCHUPS, MEMBERS, propose, sunday_ids
from test_contest_flow import FROZEN_NOW as FRIDAY
from test_survivor_api import FROZEN_NOW as THURSDAY
from test_survivor_api import WEEK1_SUNDAY

WEEK1_DEADLINE = contest.pick_deadline(1)  # Sat Sep 12 4 PM PT
AFTER_DEADLINE = WEEK1_DEADLINE + timedelta(hours=3)
SATURDAY_2PM = WEEK1_DEADLINE - timedelta(hours=2)


def _seed(tmp_path, monkeypatch, matchups, now):
    nfl_db = tmp_path / "nfl-odds.sqlite"
    storage = Storage(nfl_db)
    ids = {}
    try:
        for away, home, start in matchups:
            go = make_nfl_spread_odds(
                {"circa": -3.0, "draftkings": -2.5},
                FETCH_AT,
                away=away,
                home=home,
                start_time=start,
            )
            storage.store([go])
            ids[f"{away}@{home}"] = go.game.game_id
    finally:
        storage.close()
    monkeypatch.setenv("NFL_ODDS_DB", str(nfl_db))
    monkeypatch.setenv("CONTEST_DB", str(tmp_path / "contest.sqlite"))
    monkeypatch.setenv("CONTEST_MEMBERS", MEMBERS)
    monkeypatch.setattr(contest_api, "_now", lambda: now)
    return ids


@pytest.fixture
def flow_env(tmp_path, monkeypatch):
    """The Million flow tests' week 1: five Sunday games + a Thursday game, Friday noon."""
    return _seed(tmp_path, monkeypatch, MATCHUPS, FRIDAY)


@pytest.fixture
def client(flow_env):
    return TestClient(contest_api.app, raise_server_exceptions=False)


def card_body(env, **extra):
    return {
        "week": 1,
        "member": "vijai",
        "picks": [{"game_id": g, "side": "home"} for g in sunday_ids(env)],
        **extra,
    }


# --- Million card ------------------------------------------------------------------


def test_card_before_deadline_is_unchanged(client, flow_env):
    r = client.post("/api/contest/card", json=card_body(flow_env))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["recorded_late"] is False
    assert body["submitted_at"] == body["locked_at"] == contest_api._pt(FRIDAY)


def test_card_after_deadline_needs_late_then_records_the_proxy_submission(
    client,
    flow_env,
    monkeypatch,
):
    monkeypatch.setattr(contest_api, "_now", lambda: AFTER_DEADLINE)
    refused = client.post("/api/contest/card", json=card_body(flow_env))
    assert refused.status_code == 409
    assert "late=true" in refused.json()["detail"]

    r = client.post("/api/contest/card", json=card_body(flow_env, late=True))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["recorded_late"] is True
    assert body["locked_at"] == contest_api._pt(AFTER_DEADLINE)
    assert body["submitted_at"] == contest_api._pt(WEEK1_DEADLINE)  # unknown -> deadline
    assert client.get("/api/contest/card", params={"week": 1}).json()["recorded_late"] is True
    # Board and season see a locked card like any other.
    assert client.get("/api/contest/board", params={"week": 1}).json()["card_locked"] is True


def test_card_late_with_the_proxys_time(client, flow_env, monkeypatch):
    monkeypatch.setattr(contest_api, "_now", lambda: AFTER_DEADLINE)
    r = client.post(
        "/api/contest/card",
        json=card_body(flow_env, late=True, submitted_at=SATURDAY_2PM.isoformat()),
    )
    assert r.status_code == 201, r.text
    assert r.json()["submitted_at"] == contest_api._pt(SATURDAY_2PM)
    assert r.json()["recorded_late"] is True


def test_card_submission_time_cannot_be_after_the_deadline(
    client,
    flow_env,
    monkeypatch,
):
    monkeypatch.setattr(contest_api, "_now", lambda: AFTER_DEADLINE)
    claimed = WEEK1_DEADLINE + timedelta(minutes=1)
    r = client.post(
        "/api/contest/card", json=card_body(flow_env, late=True, submitted_at=claimed.isoformat())
    )
    assert r.status_code == 422
    assert "after the effective deadline" in r.json()["detail"]
    naive = client.post(
        "/api/contest/card",
        json=card_body(flow_env, late=True, submitted_at="2026-09-12T14:00:00"),
    )
    assert naive.status_code == 422 and "timezone" in naive.json()["detail"]
    assert client.get("/api/contest/card", params={"week": 1}).status_code == 404


def test_rule8_still_bounds_a_late_record(client, flow_env, monkeypatch):
    """A card with the Thursday game is due at that kickoff; a late record may
    claim any submission time up to the kickoff, but not after it."""
    thursday = flow_env["DET@PHI"]
    picks = [{"game_id": g, "side": "home"} for g in sunday_ids(flow_env)[:4] + [thursday]]
    kickoff = datetime(2026, 9, 11, 0, 15, tzinfo=UTC)
    monkeypatch.setattr(contest_api, "_now", lambda: FRIDAY)  # after the Thursday kickoff
    too_late = client.post(
        "/api/contest/card",
        json={
            "week": 1,
            "member": "vijai",
            "picks": picks,
            "late": True,
            "submitted_at": (kickoff + timedelta(minutes=5)).isoformat(),
        },
    )
    assert too_late.status_code == 422
    ok = client.post(
        "/api/contest/card",
        json={
            "week": 1,
            "member": "vijai",
            "picks": picks,
            "late": True,
            "submitted_at": (kickoff - timedelta(hours=1)).isoformat(),
        },
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["effective_deadline"] == contest_api._pt(kickoff)


def test_late_pick_prompts_on_the_ui_button(client, flow_env):
    """The consensus payload carries the effective deadline the UI compares
    against to switch the lock button into 'record the proxy's card' mode."""
    for m in ("vijai", "sam", "alex"):
        propose(client, m, [(g, "home") for g in sunday_ids(flow_env)])
    c = client.get("/api/contest/consensus", params={"week": 1, "member": "vijai"}).json()
    assert c["effective_deadline"] == contest_api._pt(WEEK1_DEADLINE)


def test_confirmation_is_optional_free_text(client, flow_env):
    client.post("/api/contest/card", json=card_body(flow_env))
    assert client.get("/api/contest/card", params={"week": 1}).json()["etsn"] is None
    r = client.patch("/api/contest/card", json={"week": 1, "etsn": "photo from proxy 9/12"})
    assert r.status_code == 200 and r.json()["etsn"] == "photo from proxy 9/12"


def test_calibration_anchors_on_the_submission_not_the_recording(tmp_path, monkeypatch):
    """Market at submission: -3 (edge 0). Market when recorded, hours later:
    -6 (edge +3). The at-lock edge must be the submission-time one."""
    nfl_db = tmp_path / "nfl.sqlite"
    storage = Storage(nfl_db)
    try:
        early = make_nfl_spread_odds(
            {"circa": -3.0},
            SATURDAY_2PM - timedelta(hours=1),
            start_time=datetime(2026, 9, 13, 17, 0, tzinfo=UTC),
        )
        late_move = make_nfl_spread_odds(
            {"circa": -6.0},
            AFTER_DEADLINE - timedelta(minutes=30),
            start_time=datetime(2026, 9, 13, 17, 0, tzinfo=UTC),
        )
        storage.store([early])
        storage.store([late_move])
        game_id = early.game.game_id
        storage.close()
        store = contest.ContestStore(tmp_path / "contest.sqlite")
        store.set_line(1, game_id, -3.0, entered_at=FRIDAY)
        picks = [(game_id, "home")] + [(f"2026-09-13-X{i}-Y{i}-1", "home") for i in range(4)]
        store.lock_card(
            1, picks, locked_by="vijai", locked_at=AFTER_DEADLINE, submitted_at=SATURDAY_2PM
        )
        store.record_results(1, {game_id: "win"})
        card = store.card(1)
        assert card is not None and card.recorded_late and card.submission_time == SATURDAY_2PM
        odds = Storage(nfl_db, read_only=True)
        buckets = {b.label: b for b in contest.calibration_report(odds, store)}
        odds.close()
        store.close()
    finally:
        pass
    assert buckets["0 <= edge < 1"].n == 1  # -3 vs -3 at submission
    assert buckets["edge >= 2"].n == 0  # not the -6 seen when recorded


def test_cards_recorded_before_d043_read_back_as_on_time(tmp_path):
    store = contest.ContestStore(tmp_path / "contest.sqlite")
    try:
        picks = [(f"2026-09-13-A{i}-B{i}-1", "home") for i in range(5)]
        store.lock_card(1, picks, locked_by="sam", locked_at=FRIDAY)
        store._conn.execute("UPDATE cards SET submitted_at = NULL")  # the pre-D-043 shape
        store._conn.commit()
        card = store.card(1)
    finally:
        store.close()
    assert card is not None
    assert card.submitted_at is None
    assert card.submission_time == FRIDAY and card.recorded_late is False


# --- Survivor pick ------------------------------------------------------------------


@pytest.fixture
def sclient(tmp_path, monkeypatch):
    """The survivor API tests' week 1 (KC@LAC Sunday), Thursday noon."""
    _seed(tmp_path, monkeypatch, [("KC", "LAC", WEEK1_SUNDAY)], THURSDAY)
    return TestClient(contest_api.app, raise_server_exceptions=False)


LEG1_DEADLINE = survivor.leg("1").deadline
SUNDAY_MORNING = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)  # after deadline, before kickoff


def survivor_pick(client, **extra):
    for m in ("vijai", "sam", "alex"):
        client.post(
            "/api/survivor/proposal",
            json={"leg": "1", "member": m, "choices": [{"team": "LAC"}]},
        )
    return client.post(
        "/api/survivor/pick", json={"leg": "1", "member": "vijai", "team": "LAC", **extra}
    )


def test_survivor_pick_after_deadline_needs_late(sclient, monkeypatch):
    monkeypatch.setattr(contest_api, "_now", lambda: SUNDAY_MORNING)
    refused = survivor_pick(sclient)
    assert refused.status_code == 409 and "late=true" in refused.json()["detail"]
    # Without a pick the entry reads as eliminated for missing the deadline...
    assert sclient.get("/api/survivor/status").json()["entry"]["alive"] is False

    r = survivor_pick(sclient, late=True)
    assert r.status_code == 201, r.text
    pick = r.json()["pick"]
    assert pick["recorded_late"] is True
    assert pick["submitted_at"] == contest_api._pt(LEG1_DEADLINE)
    assert pick["locked_at"] == contest_api._pt(SUNDAY_MORNING)
    # ...and the late record revives it: the proxy did submit in time.
    status = sclient.get("/api/survivor/status").json()
    assert status["entry"]["alive"] is True and status["used"] == {"LAC": "1"}


def test_survivor_pick_late_with_time_and_bounds(sclient, monkeypatch):
    monkeypatch.setattr(contest_api, "_now", lambda: SUNDAY_MORNING)
    bad = survivor_pick(
        sclient, late=True, submitted_at=(LEG1_DEADLINE + timedelta(minutes=1)).isoformat()
    )
    assert bad.status_code == 422
    when = LEG1_DEADLINE - timedelta(hours=5)
    r = survivor_pick(sclient, late=True, submitted_at=when.isoformat())
    assert r.status_code == 201, r.text
    assert r.json()["pick"]["submitted_at"] == contest_api._pt(when)
    assert sclient.get("/api/survivor/pick", params={"leg": "1"}).json()["recorded_late"] is True


def test_survivor_pick_on_time_is_unchanged(sclient):
    r = survivor_pick(sclient)
    assert r.status_code == 201, r.text
    pick = r.json()["pick"]
    assert pick["recorded_late"] is False
    assert pick["submitted_at"] == pick["locked_at"] == contest_api._pt(THURSDAY)
