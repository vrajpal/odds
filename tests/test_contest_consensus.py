"""C2/C3 domain tests: blind proposals, stance tallies, Rule-8 deadlines,
card rules, grading, and the full-season tiebreaker ladder."""

from datetime import UTC, datetime, timedelta

import pytest

from mlb_odds.contest import (
    PACIFIC,
    Card,
    CardPick,
    ContestStore,
    Proposal,
    Vote,
    booby_guard_alert,
    captain_for,
    effective_deadline,
    pick_deadline,
    season_summary,
    tally_candidates,
    week_score,
    week_window,
)

MEMBERS = ["vijai", "sam", "alex"]
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def prop(member, game, side, week=1, note=""):
    return Proposal(week=week, member=member, game_id=game, side=side, note=note)


def vote(member, game, side, week=1):
    return Vote(week=week, member=member, game_id=game, side=side)


# --- Captain rotation ---


def test_captain_rotates_in_member_order():
    assert captain_for(1, MEMBERS) == "vijai"
    assert captain_for(2, MEMBERS) == "sam"
    assert captain_for(3, MEMBERS) == "alex"
    assert captain_for(4, MEMBERS) == "vijai"
    assert captain_for(18, MEMBERS) == "alex"  # (18-1) % 3 == 2


def test_captain_requires_members():
    with pytest.raises(ValueError):
        captain_for(1, [])


# --- Rule 8 effective deadline ---


def test_effective_deadline_defaults_to_saturday():
    sunday = week_window(1)[0] + timedelta(days=4, hours=15)
    assert effective_deadline(1, [sunday]) == pick_deadline(1)


def test_effective_deadline_pulled_to_earliest_early_kickoff():
    # Thursday Night Football kicks before Saturday 4 PM PT.
    tnf = datetime(2026, 9, 11, 0, 15, tzinfo=UTC)  # Thu 5:15 PM PT
    sunday = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)
    assert effective_deadline(1, [tnf, sunday]) == tnf


def test_effective_deadline_thanksgiving_slate():
    # Week 12: Thanksgiving games Thursday morning PT pull the whole card in.
    turkey = datetime(2026, 11, 26, 17, 30, tzinfo=UTC)  # Thu 9:30 AM PT
    assert effective_deadline(12, [turkey]) == turkey
    assert effective_deadline(12, []) == pick_deadline(12)


def test_saturday_kickoff_after_deadline_does_not_pull():
    # A Saturday 5:30 PM PT game is after the 4 PM deadline — no Rule 8 pull.
    sat_night = datetime(2026, 9, 13, 0, 30, tzinfo=UTC)  # Sat 5:30 PM PT
    assert effective_deadline(1, [sat_night]) == pick_deadline(1)


# --- Stance tally ---


def test_unanimous_when_all_members_back_same_side():
    proposals = [prop(m, "g1", "home") for m in MEMBERS]
    (cand,) = tally_candidates(proposals, [], MEMBERS)
    assert cand.status == "unanimous"
    assert cand.backers == ("vijai", "sam", "alex")


def test_majority_and_contested():
    proposals = [
        prop("vijai", "g1", "home"),
        prop("sam", "g1", "home"),
        prop("alex", "g2", "away"),
    ]
    by_key = {(c.game_id, c.side): c for c in tally_candidates(proposals, [], MEMBERS)}
    assert by_key[("g1", "home")].status == "majority"
    assert by_key[("g2", "away")].status == "contested"


def test_vote_overrides_own_proposal():
    # sam proposed g1 home but later voted g1 away: one stance per member.
    proposals = [prop("vijai", "g1", "home"), prop("sam", "g1", "home")]
    votes = [vote("sam", "g1", "away")]
    by_key = {(c.game_id, c.side): c for c in tally_candidates(proposals, votes, MEMBERS)}
    assert by_key[("g1", "home")].backers == ("vijai",)
    assert by_key[("g1", "away")].backers == ("sam",)


def test_vote_can_create_unanimity():
    proposals = [prop("vijai", "g1", "home"), prop("sam", "g1", "home")]
    votes = [vote("alex", "g1", "home")]
    (cand,) = tally_candidates(proposals, votes, MEMBERS)
    assert cand.status == "unanimous"


def test_candidates_ordered_by_backing():
    proposals = [
        prop("vijai", "g1", "home"),
        prop("sam", "g1", "home"),
        prop("alex", "g1", "home"),
        prop("vijai", "g2", "away"),
        prop("sam", "g2", "away"),
        prop("vijai", "g3", "home"),
    ]
    cands = tally_candidates(proposals, [], MEMBERS)
    assert [c.game_id for c in cands] == ["g1", "g2", "g3"]


# --- Store: blind proposals, votes, cards ---


@pytest.fixture
def store(tmp_path):
    s = ContestStore(tmp_path / "contest.sqlite")
    yield s
    s.close()


def test_blind_submission_is_one_shot(store):
    store.submit_proposals(1, "vijai", [("g1", "home", "like the number")], submitted_at=NOW)
    assert store.has_submitted(1, "vijai")
    assert not store.has_submitted(1, "sam")
    with pytest.raises(ValueError, match="already submitted"):
        store.submit_proposals(1, "vijai", [("g2", "away", "")], submitted_at=NOW)
    assert store.submitted_members(1) == ["vijai"]


def test_proposals_validation(store):
    with pytest.raises(ValueError, match="1-5"):
        store.submit_proposals(1, "vijai", [], submitted_at=NOW)
    with pytest.raises(ValueError, match="1-5"):
        store.submit_proposals(
            1, "vijai", [(f"g{i}", "home", "") for i in range(6)], submitted_at=NOW
        )
    with pytest.raises(ValueError, match="duplicate"):
        store.submit_proposals(
            1, "vijai", [("g1", "home", ""), ("g1", "away", "")], submitted_at=NOW
        )


def test_vote_upsert_keeps_latest_stance(store):
    store.cast_vote(1, "sam", "g1", "home", cast_at=NOW)
    store.cast_vote(1, "sam", "g1", "away", cast_at=NOW + timedelta(minutes=5))
    (v,) = store.votes(1)
    assert v.side == "away"


def test_card_lock_rules(store):
    picks = [(f"g{i}", "home") for i in range(5)]
    store.lock_card(1, picks, locked_by="vijai", locked_at=NOW)
    card = store.card(1)
    assert card is not None
    assert len(card.picks) == 5
    assert card.locked_by == "vijai"
    assert card.etsn is None

    with pytest.raises(ValueError, match="already locked"):
        store.lock_card(1, picks, locked_by="sam", locked_at=NOW)
    with pytest.raises(ValueError, match="exactly 5"):
        store.lock_card(2, picks[:4], locked_by="sam", locked_at=NOW)
    with pytest.raises(ValueError, match="duplicate"):
        store.lock_card(
            2, [("g1", "home")] * 2 + [("g2", "home"), ("g3", "home"), ("g4", "home")],
            locked_by="sam", locked_at=NOW,
        )


def test_etsn_roundtrip(store):
    with pytest.raises(ValueError, match="no locked card"):
        store.set_etsn(1, "123456789012")
    store.lock_card(1, [(f"g{i}", "away") for i in range(5)], locked_by="alex", locked_at=NOW)
    store.set_etsn(1, "123456789012")
    card = store.card(1)
    assert card is not None and card.etsn == "123456789012"


def test_grading_validation_and_scoring(store):
    store.lock_card(1, [(f"g{i}", "home") for i in range(5)], locked_by="vijai", locked_at=NOW)
    with pytest.raises(ValueError, match="not on the week 1 card"):
        store.record_results(1, {"nope": "win"})
    with pytest.raises(ValueError, match="win/loss/push"):
        store.record_results(1, {"g0": "covered"})
    store.record_results(1, {"g0": "win", "g1": "win", "g2": "push", "g3": "loss"})
    card = store.card(1)
    assert card is not None
    score = week_score(card)
    assert (score.wins, score.losses, score.pushes, score.graded) == (2, 1, 1, 4)
    assert score.points == 2.5
    # Correction: re-entry overwrites.
    store.record_results(1, {"g3": "win"})
    card = store.card(1)
    assert card is not None and week_score(card).points == 3.5


# --- Season summary and the tiebreaker ladder ---


def graded_card(week: int, results: list[str]) -> Card:
    return Card(
        week=week,
        picks=tuple(
            CardPick(game_id=f"w{week}g{i}", side="home", result=r)
            for i, r in enumerate(results)
        ),
        locked_by="vijai",
        locked_at=NOW,
        etsn=None,
    )


def test_tiebreaker_ladder_counts_each_rung():
    cards = [
        graded_card(1, ["win"] * 5),                                  # 5-0
        graded_card(2, ["win", "win", "win", "win", "push"]),          # 4-0-1
        graded_card(3, ["win", "win", "win", "win", "loss"]),          # 4-1
        graded_card(4, ["win", "win", "win", "loss", "loss"]),         # 3-2 winning week
        graded_card(5, ["push"] * 5),                                  # 2.5 — not a winning week
        graded_card(6, ["loss"] * 5),                                  # 0-5
    ]
    scores, ladder, quarters, _ = season_summary(cards, now=NOW)
    assert ladder.total_wins == 5 + 4 + 4 + 3
    # winning week is strictly > 2.5 points: weeks 1-4 qualify, week 5 (2.5) doesn't.
    assert ladder.winning_weeks == 4
    assert ladder.weeks_5_0 == 1
    assert ladder.weeks_4_0_1 == 1
    assert ladder.weeks_4_1 == 1
    assert quarters[1] == 5 + 4.5 + 4.0 + 3.0  # weeks 1-4
    assert quarters[2] == 2.5 + 0  # weeks 5-9
    assert quarters[3] == 0 and quarters[4] == 0
    assert sum(s.points for s in scores) == 19.0


def test_booby_eligibility_requires_five_picks_every_completed_week():
    # As of mid-week-3, weeks 1 and 2 are complete.
    now = week_window(3)[0] + timedelta(days=2)
    full = [graded_card(1, ["win"] * 5), graded_card(2, ["loss"] * 5)]
    assert season_summary(full, now=now)[3] is True
    # Missing week 2 entirely → permanently ineligible.
    assert season_summary([full[0]], now=now)[3] is False
    # Week 3 not yet complete, so its absence doesn't disqualify.
    assert season_summary(full, now=now)[3] is True


def test_booby_guard_alert_fires_saturday_10am():
    week1_sat_9am = datetime(2026, 9, 12, 9, 0, tzinfo=PACIFIC)
    week1_sat_11am = datetime(2026, 9, 12, 11, 0, tzinfo=PACIFIC)
    assert not booby_guard_alert(1, card_locked=False, now=week1_sat_9am)
    assert booby_guard_alert(1, card_locked=False, now=week1_sat_11am)
    assert not booby_guard_alert(1, card_locked=True, now=week1_sat_11am)
    # Not this contest week → never alerts.
    assert not booby_guard_alert(2, card_locked=False, now=week1_sat_11am)
