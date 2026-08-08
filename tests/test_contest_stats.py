"""C4.2-C4.5 stats tests: CLV math and report, calibration buckets, power
ratings recovered from synthetic spreads, rest/divisional context, member
records including mirrored grading."""

from datetime import UTC, datetime, timedelta

import pytest

from conftest import make_nfl_spread_odds
from mlb_odds.contest import (
    ContestStore,
    calibration_report,
    clv_report,
    game_context,
    member_stats,
    pick_side_value,
    power_ratings,
    predicted_home_spread,
    rest_days,
)
from mlb_odds.storage import Storage

FETCH_T0 = datetime(2026, 9, 10, 17, 0, tzinfo=UTC)  # Thursday of week 1
FETCH_T1 = datetime(2026, 9, 12, 17, 0, tzinfo=UTC)  # Saturday morning
KICKOFF = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)  # Sunday
LOCKED_AT = datetime(2026, 9, 12, 20, 0, tzinfo=UTC)


# --- side-value convention (edge and CLV share it) ---


def test_pick_side_value_conventions():
    # Took home -2.5; market says home should be -4.5: home side has value.
    assert pick_side_value("home", -2.5, -4.5) == 2.0
    assert pick_side_value("away", -2.5, -4.5) == -2.0
    # Took away +3.5 (home -3.5); close was home -2.5 (fair away +2.5):
    # got one more point than fair -> +1 for the away side.
    assert pick_side_value("away", -3.5, -2.5) == 1.0
    # And the mirror: away +2.5 taken when fair away is +3.5 -> -1.
    assert pick_side_value("away", -2.5, -3.5) == -1.0
    with pytest.raises(ValueError):
        pick_side_value("over", -2.5, -3.5)


# --- CLV report ---


@pytest.fixture
def stores(tmp_path):
    odds = Storage(tmp_path / "nfl-odds.sqlite")
    store = ContestStore(tmp_path / "contest.sqlite")
    yield odds, store
    store.close()
    odds.close()


def seed_game(odds, away, home, *, open_spread, close_spread, start=KICKOFF):
    for t, spread in [(FETCH_T0, open_spread), (FETCH_T1, close_spread)]:
        go = make_nfl_spread_odds({"circa": spread}, t, away=away, home=home, start_time=start)
        odds.store([go], changed_only=True)
    return go.game.game_id


def test_clv_report_signs_and_aggregation(stores):
    odds, store = stores
    # Home pick: took -2.5, closed -4.5 -> +2 CLV. Away pick: took away +3.5
    # (home -3.5), closed home -2.5 (fair away +2.5) -> +1 CLV. The negative
    # case is pinned by test_pick_side_value_conventions.
    g1 = seed_game(odds, "KC", "LAC", open_spread=-2.5, close_spread=-4.5)
    g2 = seed_game(odds, "BUF", "MIA", open_spread=-3.5, close_spread=-2.5)
    entered = FETCH_T0 + timedelta(hours=1)
    store.set_line(1, g1, -2.5, entered_at=entered)
    store.set_line(1, g2, -3.5, entered_at=entered)
    store.lock_card(
        1,
        [(g1, "home"), (g2, "away")] + [(f"g{i}", "home") for i in range(3)],
        locked_by="vijai",
        locked_at=LOCKED_AT,
    )
    store.record_results(1, {g1: "win", g2: "loss"})

    rows = {r.game_id: r for r in clv_report(odds, store)}
    assert rows[g1].clv == 2.0 and rows[g1].closing == -4.5 and rows[g1].result == "win"
    assert rows[g2].clv == 1.0
    # Filler picks with no line/game are excluded entirely.
    assert set(rows) == {g1, g2}


def test_clv_none_when_no_prestart_snapshot(stores):
    odds, store = stores
    # Only snapshot is after kickoff: no closing line exists.
    go = make_nfl_spread_odds(
        {"circa": -3.0}, KICKOFF + timedelta(hours=1), away="SF", home="SEA",
        start_time=KICKOFF,
    )
    odds.store([go])
    gid = go.game.game_id
    store.set_line(1, gid, -3.0, entered_at=KICKOFF)
    store.lock_card(
        1, [(gid, "home")] + [(f"g{i}", "home") for i in range(4)],
        locked_by="sam", locked_at=LOCKED_AT,
    )
    (row,) = clv_report(odds, store)
    assert row.closing is None and row.clv is None


# --- calibration ---


def test_calibration_buckets_and_key_numbers(stores):
    odds, store = stores
    # Edges at lock (locked Sat 20:00 UTC, after FETCH_T1): market = close.
    # g1: line -2.5 vs market -4.5, home pick -> edge +2.0 (>=2, crosses 3)
    # g2: line -2.5 vs market -3.5, AWAY pick -> edge -1.0 (<0, crosses 3):
    #     away gets +2.5 from the contest when fair is +3.5 — against the signal
    # g3: line -3.0 vs market -3.5, home pick -> edge +0.5 ([0,1))
    g1 = seed_game(odds, "KC", "LAC", open_spread=-2.5, close_spread=-4.5)
    g2 = seed_game(odds, "BUF", "MIA", open_spread=-2.0, close_spread=-3.5)
    g3 = seed_game(odds, "DAL", "NYG", open_spread=-3.0, close_spread=-3.5)
    entered = FETCH_T0 + timedelta(hours=1)
    store.set_line(1, g1, -2.5, entered_at=entered)
    store.set_line(1, g2, -2.5, entered_at=entered)
    store.set_line(1, g3, -3.0, entered_at=entered)
    store.lock_card(
        1,
        [(g1, "home"), (g2, "away"), (g3, "home"), ("gx", "home"), ("gy", "home")],
        locked_by="vijai",
        locked_at=LOCKED_AT,
    )
    store.record_results(1, {g1: "win", g2: "loss", g3: "push"})

    buckets = {b.label: b for b in calibration_report(odds, store)}
    assert (buckets["edge >= 2"].wins, buckets["edge >= 2"].n) == (1, 1)
    assert buckets["edge >= 2"].cover_rate == 1.0
    assert (buckets["edge < 0"].losses, buckets["edge < 0"].n) == (1, 1)
    assert buckets["0 <= edge < 1"].pushes == 1
    assert buckets["0 <= edge < 1"].cover_rate is None  # no decided picks
    # g1 (-2.5 vs -4.5) and g2 (-3.5 vs -2.5) both cross 3; g3 does not.
    assert buckets["key number crossed"].n == 2


# --- power ratings ---


def test_power_ratings_recovered_from_synthetic_market(tmp_path):
    odds = Storage(tmp_path / "nfl-odds.sqlite")
    true = {"KC": 6.0, "BUF": 2.0, "SF": -2.0, "DET": -6.0}
    hfa = 2.0
    teams = list(true)
    start = KICKOFF
    for home in teams:
        for away in teams:
            if home == away:
                continue
            spread = -(true[home] - true[away] + hfa)
            go = make_nfl_spread_odds(
                {"circa": spread}, FETCH_T0, away=away, home=home, start_time=start
            )
            odds.store([go])
            start += timedelta(hours=4)

    fitted = power_ratings(odds)
    assert fitted is not None
    ratings, fit_hfa = fitted
    # Ridge shrinks magnitudes; order and spacing direction must survive.
    assert sorted(ratings, key=lambda t: -ratings[t]) == ["KC", "BUF", "SF", "DET"]
    assert abs(fit_hfa - hfa) < 1.0
    predicted = predicted_home_spread(ratings, fit_hfa, "KC", "DET")
    assert predicted is not None
    assert abs(predicted - (-(true["KC"] - true["DET"] + hfa))) < 2.0
    assert predicted_home_spread(ratings, fit_hfa, "KC", "XXX") is None
    odds.close()


def test_power_ratings_need_enough_games(tmp_path):
    odds = Storage(tmp_path / "nfl-odds.sqlite")
    odds.store([make_nfl_spread_odds({"circa": -3.0}, FETCH_T0)])
    assert power_ratings(odds) is None
    odds.close()


# --- situational context ---


def test_rest_days_and_divisional_flag(tmp_path):
    odds = Storage(tmp_path / "nfl-odds.sqlite")
    week1 = KICKOFF
    thursday2 = KICKOFF + timedelta(days=4)  # short week for both
    week3 = KICKOFF + timedelta(days=14)  # KC idle in week 2 -> long rest
    for away, home, start in [
        ("KC", "LAC", week1),
        ("LAC", "DEN", thursday2),
        ("KC", "SF", week3),
    ]:
        odds.store([make_nfl_spread_odds({"circa": -3.0}, FETCH_T0, away=away,
                                         home=home, start_time=start)])
    games = odds.games()
    assert rest_days(games, "LAC", thursday2) == 4  # short week
    assert rest_days(games, "KC", week3) == 14  # coming off idle week
    assert rest_days(games, "KC", week1) is None  # first stored game

    by_id = {g.game_id: g for g in games}
    div_game = next(g for g in games if g.home_team == "LAC" and g.away_team == "KC")
    assert game_context(games, div_game).divisional is True  # both AFC West
    cross = next(g for g in games if g.home_team == "SF")
    ctx = game_context(games, cross)
    assert ctx.divisional is False
    assert ctx.away_rest == 14
    odds.close()
    assert by_id  # silence unused warning paths


# --- member stats ---


def test_member_stats_mirrored_grading_and_captaincy(tmp_path):
    store = ContestStore(tmp_path / "contest.sqlite")
    members = ["vijai", "sam", "alex"]
    now = LOCKED_AT
    # vijai proposes g1 home; sam proposes g1 away; alex never proposes g1
    # but votes g1 home. Card takes g1 home; result win.
    store.submit_proposals(1, "vijai", [("g1", "home", "")], submitted_at=now)
    store.submit_proposals(1, "sam", [("g1", "away", ""), ("g2", "home", "")],
                           submitted_at=now)
    store.cast_vote(1, "alex", "g1", "home", cast_at=now)
    store.lock_card(
        1, [("g1", "home"), ("g2", "home"), ("g3", "home"), ("g4", "home"), ("g5", "home")],
        locked_by="vijai", locked_at=now,
    )
    store.record_results(1, {"g1": "win", "g2": "push"})

    stats = {s.member: s for s in member_stats(store, members)}
    # vijai: proposed g1 home -> win.
    assert (stats["vijai"].proposal_wins, stats["vijai"].proposal_losses) == (1, 0)
    # sam: g1 away mirrors card's win into a loss; g2 push stays push.
    assert (stats["sam"].proposal_wins, stats["sam"].proposal_losses,
            stats["sam"].proposal_pushes) == (0, 1, 1)
    # alex: stance-only on g1 (vote) -> win; no proposals.
    assert (stats["alex"].stance_wins, stats["alex"].proposal_wins) == (1, 0)
    # Week 1 captain is vijai; card points = 1 + 0.5.
    assert stats["vijai"].captain_weeks == 1
    assert stats["vijai"].captain_points == 1.5
    assert stats["sam"].captain_weeks == 0
    store.close()
