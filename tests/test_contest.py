"""Contest module tests. The load-bearing part is movement math: consensus
"as of" a timestamp must be correct against an append-only odds table,
including under --changed-only dedup where absent rows mean "unchanged"."""

from datetime import UTC, datetime, timedelta

import pytest

from conftest import make_game_odds, make_nfl_spread_odds
from mlb_odds.contest import (
    PACIFIC,
    BoardGame,
    ContestStore,
    book_spreads,
    build_board,
    consensus,
    game_context,
    key_numbers_crossed,
    lines_post_time,
    pick_deadline,
    rest_days,
    spread_history,
    week_of,
    week_window,
)
from mlb_odds.models import Game, Quote
from mlb_odds.storage import Storage


def pt(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=PACIFIC)


# --- Contest calendar (Rule 19 weeks, Rule 7 deadlines) ---


def test_week1_window_bounds():
    start, end = week_window(1)
    assert start == pt(2026, 9, 9, 2)
    assert end == pt(2026, 9, 16, 2)


def test_week_of_boundaries_are_half_open():
    start, end = week_window(1)
    assert week_of(start) == 1
    assert week_of(start - timedelta(microseconds=1)) is None
    assert week_of(end) == 2
    assert week_of(end - timedelta(microseconds=1)) == 1


def test_week18_ends_at_contest_end_instant():
    # Rules: contest ends 1:59 AM Wed Jan 13, 2027 — window is [.., Jan 13 2:00).
    _, end = week_window(18)
    assert end == pt(2027, 1, 13, 2)
    assert week_of(end - timedelta(minutes=1)) == 18
    assert week_of(end) is None


def test_dst_fallback_week_is_169_hours_wall_clock_2am():
    # Week 8 (Oct 28 - Nov 4) contains the Nov 1 fall-back: both bounds must
    # stay 2:00 AM local while the week gains an hour of real time.
    start, end = week_window(8)
    assert start.date().isoformat() == "2026-10-28"
    assert (start.hour, end.hour) == (2, 2)
    # Same-zone aware subtraction is wall-clock (7 days); true elapsed time
    # needs a UTC detour and is an hour longer, straddling PDT->PST.
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=169)
    assert start.utcoffset() == timedelta(hours=-7)  # PDT
    assert end.utcoffset() == timedelta(hours=-8)  # PST


def test_week_window_rejects_out_of_range():
    for bad in (0, 19):
        with pytest.raises(ValueError):
            week_window(bad)


def test_week_of_requires_aware():
    with pytest.raises(ValueError):
        week_of(datetime(2026, 9, 10))


def test_pick_deadline_is_saturday_4pm_pt():
    assert pick_deadline(1) == pt(2026, 9, 12, 16)
    # Thanksgiving week's deadline is still the Saturday (Rule 8 card-level
    # early deadlines are C2 scope).
    assert pick_deadline(12) == pt(2026, 11, 28, 16)


def test_lines_post_thursday_normally_wednesday_on_holiday_weeks():
    assert lines_post_time(1) == pt(2026, 9, 10, 10)  # Thursday
    assert lines_post_time(12) == pt(2026, 11, 25, 10)  # Wed of Thanksgiving week
    assert lines_post_time(16) == pt(2026, 12, 23, 10)  # Wed of Christmas week
    assert lines_post_time(17) == pt(2026, 12, 31, 10)  # back to Thursday


# --- ContestStore ---


def test_store_roundtrip_and_upsert(tmp_path):
    db = tmp_path / "contest.sqlite"
    store = ContestStore(db)
    t1 = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
    store.set_line(1, "2026-09-13-KC-LAC-1", -2.5, entered_at=t1)

    lines = store.lines(1)
    assert lines["2026-09-13-KC-LAC-1"].home_spread == -2.5
    assert lines["2026-09-13-KC-LAC-1"].entered_at == t1

    # Upsert overwrites and re-anchors entered_at (typo correction).
    t2 = t1 + timedelta(hours=1)
    store.set_line(1, "2026-09-13-KC-LAC-1", -3.5, entered_at=t2)
    lines = store.lines(1)
    assert lines["2026-09-13-KC-LAC-1"].home_spread == -3.5
    assert lines["2026-09-13-KC-LAC-1"].entered_at == t2
    assert store.lines(2) == {}
    store.close()

    # Survives reopen: it is a file, not process state.
    reopened = ContestStore(db)
    assert reopened.lines(1)["2026-09-13-KC-LAC-1"].home_spread == -3.5
    reopened.close()


@pytest.mark.parametrize(
    ("week", "spread"),
    [(0, -2.5), (19, -2.5), (1, -3.25), (1, 31.0)],
)
def test_store_rejects_bad_input(tmp_path, week, spread):
    store = ContestStore(tmp_path / "contest.sqlite")
    with pytest.raises(ValueError):
        store.set_line(week, "gid", spread, entered_at=datetime.now(UTC))
    store.close()


def test_store_rejects_naive_entered_at(tmp_path):
    store = ContestStore(tmp_path / "contest.sqlite")
    with pytest.raises(ValueError):
        store.set_line(1, "gid", -2.5, entered_at=datetime(2026, 9, 10))
    store.close()


# --- Movement math against a real odds database ---

T0 = datetime(2026, 9, 10, 17, 0, tzinfo=UTC)  # Thu 10:00 PT — lines post
T1 = datetime(2026, 9, 11, 18, 0, tzinfo=UTC)  # Friday
T2 = datetime(2026, 9, 12, 0, 0, tzinfo=UTC)  # Friday evening PT


@pytest.fixture
def odds_db(tmp_path):
    storage = Storage(tmp_path / "nfl-odds.sqlite")
    yield storage
    storage.close()


def seed_moving_lines(storage: Storage) -> str:
    """Three snapshots under changed_only: circa moves every cycle, draftkings
    holds at T1 (so no T1 row is written for it — that absence meaning
    "unchanged" is exactly what asof math must get right)."""
    snaps = [
        (T0, {"circa": -2.5, "draftkings": -3.0}),
        (T1, {"circa": -3.5, "draftkings": -3.0}),
        (T2, {"circa": -4.5, "draftkings": -4.0}),
    ]
    game_id = ""
    for fetched_at, lines in snaps:
        go = make_nfl_spread_odds(lines, fetched_at)
        storage.store([go], changed_only=True)
        game_id = go.game.game_id
    return game_id


def test_changed_only_dedup_confirmed(odds_db):
    # Sanity: the fixture really exercises dedup — draftkings has no T1 row.
    game_id = seed_moving_lines(odds_db)
    dk_times = {
        fetched_at
        for fetched_at, _p, book, market, outcome, *_ in odds_db.history_rows(game_id)
        if book == "draftkings" and market == "spread" and outcome == "home"
    }
    assert dk_times == {T0.isoformat(), T2.isoformat()}


def test_latest_book_spreads_and_consensus(odds_db):
    game_id = seed_moving_lines(odds_db)
    ticks = spread_history(odds_db, game_id)
    latest = book_spreads(ticks)
    assert latest == {"circa": -4.5, "draftkings": -4.0}
    assert consensus(latest) == -4.25


def test_asof_between_snapshots(odds_db):
    game_id = seed_moving_lines(odds_db)
    ticks = spread_history(odds_db, game_id)
    assert book_spreads(ticks, asof=T0 + timedelta(hours=1)) == {
        "circa": -2.5,
        "draftkings": -3.0,
    }


def test_asof_carries_unchanged_book_forward(odds_db):
    # Between T1 and T2, draftkings' newest row is still T0's: changed_only
    # wrote nothing at T1 because the line didn't move. If this returned only
    # circa, every movement number after a quiet book cycle would be wrong.
    game_id = seed_moving_lines(odds_db)
    ticks = spread_history(odds_db, game_id)
    between = book_spreads(ticks, asof=T1 + timedelta(hours=1))
    assert between == {"circa": -3.5, "draftkings": -3.0}
    assert consensus(between) == -3.25


def test_asof_before_any_snapshot_is_empty(odds_db):
    game_id = seed_moving_lines(odds_db)
    ticks = spread_history(odds_db, game_id)
    assert book_spreads(ticks, asof=T0 - timedelta(hours=1)) == {}
    assert consensus({}) is None


def test_asof_exactly_at_snapshot_includes_it(odds_db):
    game_id = seed_moving_lines(odds_db)
    ticks = spread_history(odds_db, game_id)
    assert book_spreads(ticks, asof=T0) == {"circa": -2.5, "draftkings": -3.0}


def test_stale_book_keeps_last_known_line(odds_db):
    # A book that stops reporting keeps its last number on the board — same
    # semantics as Storage.latest_odds (SPEC FR1 partial results).
    game_id = ""
    for fetched_at, lines in [
        (T0, {"circa": -2.5, "espn_bet": -2.5}),
        (T2, {"circa": -4.5}),
    ]:
        go = make_nfl_spread_odds(lines, fetched_at)
        odds_db.store([go])
        game_id = go.game.game_id
    latest = book_spreads(spread_history(odds_db, game_id))
    assert latest == {"circa": -4.5, "espn_bet": -2.5}


def test_same_book_via_two_providers_resolves_to_newest(odds_db):
    first = make_nfl_spread_odds({"circa": -3.0}, T0, provider="the_odds_api")
    second = make_nfl_spread_odds({"circa": -3.5}, T1, provider="espn")
    odds_db.store([first])
    odds_db.store([second])
    assert first.game.game_id == second.game.game_id  # converged (D-008)
    latest = book_spreads(spread_history(odds_db, first.game.game_id))
    assert latest == {"circa": -3.5}


def test_other_markets_and_outcomes_never_pollute_spread_history(odds_db):
    quotes = [
        Quote(book="circa", market="spread", outcome="home", line=-2.5, price=-110),
        Quote(book="circa", market="spread", outcome="away", line=2.5, price=-110),
        Quote(book="circa", market="moneyline", outcome="home", price=-140),
        Quote(book="circa", market="run_line", outcome="home", line=-1.5, price=-105),
        Quote(book="circa", market="total", outcome="over", line=44.5, price=-110),
        Quote(
            book="circa",
            market="batter_home_runs",
            outcome="over",
            line=0.5,
            price=200,
            player="Somebody",
        ),
    ]
    go = make_game_odds(quotes=quotes, start_time=T2 + timedelta(days=1), fetched_at=T0)
    odds_db.store([go])
    ticks = spread_history(odds_db, go.game.game_id)
    assert [(t.book, t.home_spread) for t in ticks] == [("circa", -2.5)]


# --- Edge math ---


def test_key_numbers_crossed_sign_aware():
    assert key_numbers_crossed(-2.5, -4.5) == [-3.0]
    assert key_numbers_crossed(2.5, 7.5) == [3.0, 7.0]
    assert key_numbers_crossed(-6.5, -7.5) == [-7.0]
    assert key_numbers_crossed(-1.5, 1.5) == []  # zero-cross has no key number
    assert key_numbers_crossed(-2.5, -2.5) == []


def test_key_number_landed_on_exactly_is_not_crossed():
    assert key_numbers_crossed(-3.0, -5.5) == []
    assert key_numbers_crossed(-2.5, -3.0) == []


def board_for(odds: Storage, lines: dict, week: int = 1) -> dict[str, BoardGame]:
    return {row.game_id: row for row in build_board(odds, lines, week)}


def make_lines(store: ContestStore, week: int, game_id: str, spread: float, at: datetime):
    store.set_line(week, game_id, spread, entered_at=at)
    return store.lines(week)


def test_board_edge_value_side_and_movement(odds_db, tmp_path):
    game_id = seed_moving_lines(odds_db)
    store = ContestStore(tmp_path / "contest.sqlite")
    # Contest line -2.5 entered Thursday after lines post; market closes -4.25.
    lines = make_lines(store, 1, game_id, -2.5, T0 + timedelta(hours=1))
    row = board_for(odds_db, lines)[game_id]
    assert row.contest_line == -2.5
    assert row.consensus == -4.25
    assert row.edge == 1.75  # market rates home 1.75 better than Circa charges
    assert row.value_side == "home"
    assert row.key_numbers == [-3.0]  # -3 sits between -2.5 and -4.25
    # movement: consensus at entry was median(-2.5, -3.0) = -2.75, now -4.25
    assert row.movement_since_entry == -1.5
    store.close()


def test_board_value_side_away(odds_db, tmp_path):
    game_id = seed_moving_lines(odds_db)
    store = ContestStore(tmp_path / "contest.sqlite")
    lines = make_lines(store, 1, game_id, -6.0, T2)
    row = board_for(odds_db, lines)[game_id]
    assert row.edge == -1.75  # Circa charges home 1.75 more than market
    assert row.value_side == "away"
    store.close()


def test_board_without_contest_line_still_lists_game(odds_db):
    game_id = seed_moving_lines(odds_db)
    row = board_for(odds_db, {})[game_id]
    assert row.consensus == -4.25
    assert row.contest_line is None
    assert row.edge is None
    assert row.value_side is None
    assert row.movement_since_entry is None


def test_board_line_entered_before_any_snapshot_has_no_movement(odds_db, tmp_path):
    game_id = seed_moving_lines(odds_db)
    store = ContestStore(tmp_path / "contest.sqlite")
    lines = make_lines(store, 1, game_id, -2.5, T0 - timedelta(days=1))
    row = board_for(odds_db, lines)[game_id]
    assert row.edge == 1.75  # current edge still computable
    assert row.movement_since_entry is None  # no baseline to move from
    store.close()


def test_board_only_shows_games_in_week_window(odds_db):
    in_week = make_nfl_spread_odds({"circa": -2.5}, T0)
    week2 = make_nfl_spread_odds(
        {"circa": -6.5},
        T0,
        away="BUF",
        home="MIA",
        start_time=datetime(2026, 9, 20, 17, 0, tzinfo=UTC),
    )
    odds_db.store([in_week])
    odds_db.store([week2])
    board1 = board_for(odds_db, {}, week=1)
    board2 = board_for(odds_db, {}, week=2)
    assert set(board1) == {in_week.game.game_id}
    assert set(board2) == {week2.game.game_id}


# --- Situational context: rest days / rest differential (C4.5) ---


def nfl_game(game_id, home, away, start_pt):
    return Game(
        game_id=game_id,
        start_time=start_pt.astimezone(UTC),
        home_team=home,
        away_team=away,
    )


def test_rest_days_counts_calendar_days_not_24h_periods():
    # SNF kickoff (Sun 5:20 PM PT) -> next Sunday early slot (1:25 PM PT) is
    # 6d17h of wall clock but 7 days of rest by NFL convention.
    games = [
        nfl_game("snf", "KC", "LV", pt(2026, 9, 13, 17, 20)),
        nfl_game("next", "KC", "DEN", pt(2026, 9, 20, 13, 25)),
    ]
    assert rest_days(games, "KC", pt(2026, 9, 20, 13, 25).astimezone(UTC)) == 7


def test_rest_days_short_week_and_mini_bye():
    games = [
        nfl_game("sun", "DAL", "NYG", pt(2026, 9, 13, 13, 25)),
        nfl_game("thu", "DAL", "PHI", pt(2026, 9, 17, 17, 15)),
        nfl_game("after", "DAL", "WAS", pt(2026, 9, 27, 13, 25)),
    ]
    kickoff_thu = pt(2026, 9, 17, 17, 15).astimezone(UTC)
    kickoff_after = pt(2026, 9, 27, 13, 25).astimezone(UTC)
    assert rest_days(games, "DAL", kickoff_thu) == 4  # short week
    assert rest_days(games, "DAL", kickoff_after) == 10  # Thursday mini-bye


def test_rest_days_none_without_prior_game():
    games = [nfl_game("opener", "KC", "LV", pt(2026, 9, 13, 13, 25))]
    assert rest_days(games, "KC", pt(2026, 9, 13, 13, 25).astimezone(UTC)) is None
    assert rest_days(games, "SF", pt(2026, 9, 13, 13, 25).astimezone(UTC)) is None


def test_game_context_rest_differential_and_divisional():
    matchup = nfl_game("wk3", "MIA", "BUF", pt(2026, 9, 27, 10, 0))
    games = [
        # MIA off a bye: last played 14 days ago. BUF played last Sunday.
        nfl_game("mia-prior", "MIA", "NYJ", pt(2026, 9, 13, 10, 0)),
        nfl_game("buf-prior", "NE", "BUF", pt(2026, 9, 20, 10, 0)),
        matchup,
    ]
    ctx = game_context(games, matchup)
    assert ctx.home_rest == 14
    assert ctx.away_rest == 7
    assert ctx.rest_differential == 7  # positive = home fresher
    assert ctx.divisional is True  # both AFC East


def test_game_context_rest_differential_none_when_either_side_unknown():
    matchup = nfl_game("wk2", "SEA", "SF", pt(2026, 9, 20, 13, 25))
    games = [
        nfl_game("sea-prior", "SEA", "ARI", pt(2026, 9, 13, 13, 25)),
        matchup,  # SF has no stored prior game
    ]
    ctx = game_context(games, matchup)
    assert ctx.home_rest == 7
    assert ctx.away_rest is None
    assert ctx.rest_differential is None


def test_game_context_non_divisional():
    matchup = nfl_game("wk2", "KC", "DAL", pt(2026, 9, 20, 13, 25))
    ctx = game_context([matchup], matchup)
    assert ctx.divisional is False
