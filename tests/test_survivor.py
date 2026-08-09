"""Survivor domain: the 20-leg calendar transcribed from the 2026 rules, the
one-pick store, consensus tally, and the constraint math (used teams, holiday
slates, elimination)."""

from datetime import UTC, datetime

import pytest

from conftest import make_nfl_spread_odds  # noqa: F401  (import parity with suite)
from mlb_odds import survivor
from mlb_odds.models import Game
from mlb_odds.survivor import (
    LEGS,
    PACIFIC,
    SurvivorPick,
    SurvivorProposal,
    SurvivorStore,
    SurvivorVote,
    entry_status,
    grade_survivor_pick,
    holiday_outlook,
    leg,
    leg_for,
    pick_deadline_for,
    pick_warnings,
    tally_teams,
    win_probability,
)


def _pt(*args: int) -> datetime:
    return datetime(*args, tzinfo=PACIFIC)


# --- calendar: Rules 7/11/12/13 transcription --------------------------------


def test_twenty_legs_in_deadline_order_without_overlap():
    assert len(LEGS) == 20
    assert [lg.leg_id for lg in LEGS] == (
        [str(w) for w in range(1, 12)] + ["TG", "12", "13", "14", "15", "XMAS", "16", "17", "18"]
    )
    for a, b in zip(LEGS, LEGS[1:], strict=False):
        assert a.end <= b.start, (a.leg_id, b.leg_id)
        assert a.deadline < b.deadline


def test_normal_leg_boundaries_match_million_weeks():
    week1 = leg("1")
    assert week1.start == _pt(2026, 9, 9, 2)
    assert week1.opens == _pt(2026, 9, 9, 10)
    assert week1.deadline == _pt(2026, 9, 12, 16)  # Saturday 4 PM PT


def test_thanksgiving_leg_is_rule_11_and_12a_exactly():
    tg = leg("TG")
    assert tg.start == _pt(2026, 11, 24, 2)  # Tue Nov 24 2:00 AM
    assert tg.end == _pt(2026, 11, 28, 0)  # through Fri 11:59 PM
    assert tg.opens == _pt(2026, 11, 24, 10)  # Rule 12a: Tuesday 10 AM
    assert tg.deadline == _pt(2026, 11, 25, 16)  # Wed Nov 25 4 PM
    # Week 11 is truncated where the Thanksgiving Contest Week begins.
    assert leg("11").end == tg.start
    # The week-12 fragment picks up at Sat 12:00 AM with a same-day deadline.
    frag = leg("12")
    assert frag.start == frag.opens == _pt(2026, 11, 28, 0)
    assert frag.deadline == _pt(2026, 11, 28, 16)
    assert frag.end == _pt(2026, 12, 1, 2)


def test_christmas_leg_is_rule_11_and_13_exactly():
    xmas = leg("XMAS")
    assert xmas.start == _pt(2026, 12, 22, 2)
    assert xmas.end == _pt(2026, 12, 26, 0)
    assert xmas.opens == _pt(2026, 12, 23, 10)  # no Tuesday carve-out for Christmas
    assert xmas.deadline == _pt(2026, 12, 24, 16)  # Thu Dec 24 4 PM
    assert leg("15").end == xmas.start
    frag = leg("16")
    assert frag.start == frag.opens == _pt(2026, 12, 26, 0)
    assert frag.deadline == _pt(2026, 12, 26, 16)


def test_leg_for_returns_current_or_next_upcoming():
    assert leg_for(_pt(2026, 8, 1, 12)).leg_id == "1"  # pre-season -> first leg
    assert leg_for(_pt(2026, 11, 24, 20)).leg_id == "TG"
    assert leg_for(_pt(2026, 12, 1, 12)).leg_id == "13"  # gap day -> next leg
    assert leg_for(_pt(2026, 12, 25, 10)).leg_id == "XMAS"
    assert leg_for(_pt(2027, 2, 1, 0)) is None  # season over


def test_leg_for_requires_aware_datetime():
    with pytest.raises(ValueError):
        leg_for(datetime(2026, 9, 10, 12, 0))


def test_unknown_leg_raises():
    with pytest.raises(ValueError):
        leg("19")


def test_pick_deadline_for_tightens_to_kickoff_only():
    week1 = leg("1")
    thursday = _pt(2026, 9, 10, 17, 15)  # kicks before the Saturday deadline
    sunday = _pt(2026, 9, 13, 10, 0)  # after the deadline: deadline governs
    assert pick_deadline_for(week1, thursday) == thursday
    assert pick_deadline_for(week1, sunday) == week1.deadline


# --- win probability ---------------------------------------------------------


def test_win_probability_shape():
    assert win_probability(0.0) == 0.5
    assert win_probability(-7.0) + win_probability(7.0) == pytest.approx(1.0)
    assert win_probability(-14.0) > win_probability(-3.0) > win_probability(3.0)
    assert 0.65 < win_probability(-7.0) < 0.75  # a TD favorite is ~70% SU


# --- store -------------------------------------------------------------------

T0 = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    s = SurvivorStore(tmp_path / "contest.sqlite")
    yield s
    s.close()


def test_proposals_are_blind_one_shot(store):
    store.submit_proposal("1", "vijai", "LAC", "big home number", submitted_at=T0)
    with pytest.raises(ValueError, match="already submitted"):
        store.submit_proposal("1", "vijai", "KC", "", submitted_at=T0)
    assert store.submitted_members("1") == ["vijai"]
    (p,) = store.proposals("1")
    assert (p.member, p.team, p.note) == ("vijai", "LAC", "big home number")


def test_vote_upserts_single_stance(store):
    store.cast_vote("1", "sam", "LAC", cast_at=T0)
    store.cast_vote("1", "sam", "KC", cast_at=T0)
    (v,) = store.votes("1")
    assert (v.member, v.team) == ("sam", "KC")


def test_lock_pick_enforces_one_per_leg_and_one_use_per_team(store):
    store.lock_pick("1", "LAC", "2026-09-13-KC-LAC", locked_by="vijai", locked_at=T0)
    with pytest.raises(ValueError, match="already locked"):
        store.lock_pick("1", "KC", "2026-09-13-KC-LAC", locked_by="vijai", locked_at=T0)
    with pytest.raises(ValueError, match="Rule 15a"):
        store.lock_pick("5", "LAC", "2026-10-11-LAC-LV", locked_by="vijai", locked_at=T0)
    assert store.used_teams() == {"LAC": "1"}


def test_result_and_etsn_round_trip(store):
    store.lock_pick("1", "LAC", "gid", locked_by="vijai", locked_at=T0)
    store.set_etsn("1", "123456789012")
    store.record_result("1", "loss")
    store.record_result("1", "win")  # corrections overwrite
    pick = store.pick("1")
    assert (pick.etsn, pick.result) == ("123456789012", "win")
    with pytest.raises(ValueError):
        store.record_result("1", "push")  # no pushes in survivor
    with pytest.raises(ValueError):
        store.record_result("2", "win")  # no pick locked
    with pytest.raises(ValueError):
        store.set_etsn("2", "x")


def test_store_rejects_unknown_leg_and_team(store):
    with pytest.raises(ValueError):
        store.submit_proposal("99", "vijai", "LAC", "", submitted_at=T0)
    with pytest.raises(ValueError):
        store.lock_pick("1", "LAX", "gid", locked_by="vijai", locked_at=T0)


# --- consensus tally ---------------------------------------------------------

MEMBERS = ["vijai", "sam", "alex"]


def _prop(member: str, team: str) -> SurvivorProposal:
    return SurvivorProposal(leg_id="1", member=member, team=team, note="")


def test_tally_orders_by_backing_and_labels_status():
    candidates = tally_teams(
        [_prop("vijai", "LAC"), _prop("sam", "LAC"), _prop("alex", "KC")], [], MEMBERS
    )
    assert [(c.team, c.backers, c.status) for c in candidates] == [
        ("LAC", ("vijai", "sam"), "majority"),
        ("KC", ("alex",), "contested"),
    ]


def test_vote_overrides_proposal_to_unanimous():
    candidates = tally_teams(
        [_prop("vijai", "LAC"), _prop("sam", "LAC"), _prop("alex", "KC")],
        [SurvivorVote(leg_id="1", member="alex", team="LAC")],
        MEMBERS,
    )
    (only,) = candidates
    assert (only.team, only.status) == ("LAC", "unanimous")


# --- constraint math ---------------------------------------------------------


def _pick(leg_id: str, team: str, result: str | None = None) -> SurvivorPick:
    return SurvivorPick(
        leg_id=leg_id,
        team=team,
        game_id="gid",
        locked_by="vijai",
        locked_at=T0,
        etsn=None,
        result=result,
    )


def test_pick_warnings_flag_holiday_slate_burn():
    # GB sits on both holiday slates: burning it in week 1 warns for both.
    warnings = pick_warnings("GB", "1", {}, {})
    assert [w.severity for w in warnings] == ["info", "info"]
    assert any("Thanksgiving" in w.message and "9 of 10" in w.message for w in warnings)
    assert any("Christmas" in w.message and "7 of 8" in w.message for w in warnings)
    # A non-slate team warns about nothing.
    assert pick_warnings("NYJ", "1", {}, {}) == []


def test_pick_warnings_escalate_to_fatal_at_zero_remaining():
    used = {t: "x" for t in sorted(survivor.THANKSGIVING_TEAMS - {"DET", "DAL"})}
    warnings = pick_warnings("DET", "5", used, {})
    tg = [w for w in warnings if "Thanksgiving" in w.message]
    assert [w.severity for w in tg] == ["warning"]  # one team would remain
    used["DAL"] = "y"
    (fatal,) = [w for w in pick_warnings("DET", "5", used, {}) if "Thanksgiving" in w.message]
    assert fatal.severity == "fatal"
    assert "guarantees elimination" in fatal.message


def test_pick_warnings_silent_in_or_after_the_holiday_leg():
    # Burning a Thanksgiving team IN the Thanksgiving leg is the point...
    assert all(
        "Thanksgiving" not in w.message for w in pick_warnings("DET", "TG", {}, {})
    )
    # ...and once the TG pick is locked the trap is disarmed.
    assert all(
        "Thanksgiving" not in w.message
        for w in pick_warnings("DET", "5", {}, {"TG": _pick("TG", "DAL")})
    )


def test_holiday_outlook_danger_ladder():
    outlooks = {o.leg_id: o for o in holiday_outlook({}, {})}
    assert outlooks["TG"].danger == "none" and len(outlooks["TG"].remaining) == 10
    assert outlooks["XMAS"].danger == "none" and len(outlooks["XMAS"].remaining) == 8

    used = {t: "x" for t in sorted(survivor.CHRISTMAS_TEAMS - {"SEA", "HOU"})}
    outlooks = {o.leg_id: o for o in holiday_outlook(used, {})}
    assert outlooks["XMAS"].danger == "critical"
    used["SEA"] = used["HOU"] = "y"
    outlooks = {o.leg_id: o for o in holiday_outlook(used, {})}
    assert outlooks["XMAS"].danger == "fatal" and outlooks["XMAS"].remaining == ()

    picked = {"XMAS": _pick("XMAS", "SEA")}
    outlooks = {o.leg_id: o for o in holiday_outlook(used, picked)}
    assert outlooks["XMAS"].danger == "none"  # locked pick disarms the trap


def test_entry_status_alive_and_counting():
    now = _pt(2026, 9, 20, 12)  # inside week 2
    picks = {"1": _pick("1", "LAC", "win"), "2": _pick("2", "KC")}
    status = entry_status(picks, now=now)
    assert status.alive and status.survived == 1


def test_entry_status_loss_eliminates():
    status = entry_status({"1": _pick("1", "LAC", "loss")}, now=_pt(2026, 9, 20, 12))
    assert not status.alive
    assert status.at_leg == "1"
    assert "LAC" in status.reason


def test_entry_status_missed_deadline_eliminates():
    # Week-1 deadline (Sat Sep 12 4 PM PT) passed with nothing locked.
    status = entry_status({}, now=_pt(2026, 9, 13, 12))
    assert not status.alive
    assert status.at_leg == "1"
    assert "deadline" in status.reason


# --- straight-up grading -----------------------------------------------------


def _game(away: str, home: str) -> Game:
    return Game(
        game_id="gid",
        start_time=T0,
        home_team=home,
        away_team=away,
        provider_ids={},
    )


def test_grade_survivor_pick_straight_up_and_tie_is_loss():
    game = _game("KC", "LAC")
    assert grade_survivor_pick(_pick("1", "LAC"), game, 27, 20) == "win"
    assert grade_survivor_pick(_pick("1", "KC"), game, 27, 20) == "loss"
    assert grade_survivor_pick(_pick("1", "LAC"), game, 20, 20) == "loss"  # Rule 6a
    with pytest.raises(ValueError):
        grade_survivor_pick(_pick("1", "GB"), game, 27, 20)
