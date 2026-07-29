"""Storage tests: temp SQLite file per test, round-trips through domain models."""

from datetime import UTC, date, datetime

import pytest

from conftest import make_game_odds
from mlb_odds.models import Quote
from mlb_odds.storage import MIGRATIONS, Storage


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.sqlite"


@pytest.fixture
def storage(db_path):
    s = Storage(db_path)
    yield s
    s.close()


def test_round_trip_models(storage):
    original = make_game_odds()
    written = storage.store([original])
    assert written == len(original.quotes)

    games = storage.games()
    assert len(games) == 1
    assert games[0] == original.game

    latest = storage.latest_odds()
    assert len(latest) == 1
    assert latest[0] == original


def test_reopening_reruns_migrations_idempotently(db_path):
    first = Storage(db_path)
    first.store([make_game_odds()])
    first.close()

    reopened = Storage(db_path)  # must not fail or re-apply applied migrations
    assert len(reopened.games()) == 1
    version = reopened._conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == len(MIGRATIONS)
    reopened.close()


def test_upserting_same_game_twice_does_not_duplicate(storage):
    snapshot = make_game_odds()
    storage.store([snapshot])
    storage.store([snapshot])

    assert len(storage.games()) == 1
    native_ids = storage._conn.execute("SELECT COUNT(*) FROM provider_game_ids").fetchone()[0]
    assert native_ids == 1


def test_upsert_updates_start_time(storage):
    storage.store([make_game_odds()])
    # Same game identity (same date/teams/number), but first pitch moved 30 minutes.
    moved = make_game_odds(start_time=datetime(2026, 7, 9, 23, 35, tzinfo=UTC))
    storage.store([moved])

    (game,) = storage.games()
    assert game.start_time == datetime(2026, 7, 9, 23, 35, tzinfo=UTC)


def test_two_snapshots_history_keeps_both_latest_returns_newer(storage):
    early = make_game_odds(fetched_at=datetime(2026, 7, 9, 15, 0, tzinfo=UTC))
    late = make_game_odds(fetched_at=datetime(2026, 7, 9, 16, 0, tzinfo=UTC))
    storage.store([early])
    storage.store([late])

    rows = storage.history_rows(early.game.game_id)
    assert len(rows) == len(early.quotes) + len(late.quotes)
    fetched_ats = {r[0] for r in rows}
    assert fetched_ats == {early.fetched_at.isoformat(), late.fetched_at.isoformat()}

    latest = storage.latest_odds()
    assert len(latest) == 1
    assert latest[0].fetched_at == late.fetched_at


def test_latest_odds_is_per_provider(storage):
    a = make_game_odds(provider="prov_a")
    b = make_game_odds(provider="prov_b", fetched_at=datetime(2026, 7, 9, 16, 0, tzinfo=UTC))
    storage.store([a, b])

    latest = storage.latest_odds()
    assert {go.provider for go in latest} == {"prov_a", "prov_b"}


def test_games_date_filter(storage):
    jul9 = make_game_odds(start_time=datetime(2026, 7, 9, 23, 5, tzinfo=UTC))
    jul10 = make_game_odds(
        away="BOS", home="TB", start_time=datetime(2026, 7, 10, 17, 10, tzinfo=UTC)
    )
    storage.store([jul9, jul10])

    assert [g.game_id for g in storage.games(on_date=date(2026, 7, 9))] == [jul9.game.game_id]
    assert [g.game_id for g in storage.games(on_date=date(2026, 7, 10))] == [jul10.game.game_id]
    assert storage.games(on_date=date(2026, 7, 11)) == []
    assert len(storage.games()) == 2


def test_all_rows_joins_game_context(storage):
    snapshot = make_game_odds()
    storage.store([snapshot])

    rows = storage.all_rows()
    assert len(rows) == len(snapshot.quotes)
    game_id, start_time, away, home, fetched_at, provider, *_ = rows[0]
    assert game_id == snapshot.game.game_id
    assert away == "NYM"
    assert home == "NYY"
    assert provider == snapshot.provider
    assert fetched_at == snapshot.fetched_at.isoformat()
    assert storage.all_rows(on_date=date(2000, 1, 1)) == []


def test_doubleheader_games_stay_distinct(storage):
    game1 = make_game_odds(game_number=1, start_time=datetime(2026, 7, 9, 17, 5, tzinfo=UTC))
    game2 = make_game_odds(game_number=2, start_time=datetime(2026, 7, 9, 23, 5, tzinfo=UTC))
    storage.store([game1, game2])

    ids = [g.game_id for g in storage.games()]
    assert ids == ["2026-07-09-NYM-NYY-1", "2026-07-09-NYM-NYY-2"]


def test_known_native_id_keeps_its_game_id_across_cycles(storage):
    """Game 1 of a doubleheader finished and dropped from the feed; the provider,
    seeing game 2 alone, provisionally numbers it 1. Identity must stick (FR2)."""
    game1 = make_game_odds(
        game_number=1, start_time=datetime(2026, 7, 9, 17, 5, tzinfo=UTC), native_id="native-g1"
    )
    game2 = make_game_odds(
        game_number=2, start_time=datetime(2026, 7, 9, 23, 5, tzinfo=UTC), native_id="native-g2"
    )
    storage.store([game1, game2])

    later = make_game_odds(
        game_number=1,  # what the provider computes when game 2 is alone in the feed
        start_time=datetime(2026, 7, 9, 23, 5, tzinfo=UTC),
        native_id="native-g2",
        fetched_at=datetime(2026, 7, 9, 21, 0, tzinfo=UTC),
    )
    storage.store([later])

    assert later.game.game_id == "2026-07-09-NYM-NYY-2"
    assert [g.game_id for g in storage.games()] == [
        "2026-07-09-NYM-NYY-1",
        "2026-07-09-NYM-NYY-2",
    ]
    # game 1 untouched: original start time, no odds appended from the later cycle
    (game1_row,) = [g for g in storage.games() if g.game_id.endswith("-1")]
    assert game1_row.start_time == datetime(2026, 7, 9, 17, 5, tzinfo=UTC)
    assert {r[0] for r in storage.history_rows("2026-07-09-NYM-NYY-1")} == {
        game1.fetched_at.isoformat()
    }
    assert later.fetched_at.isoformat() in {
        r[0] for r in storage.history_rows("2026-07-09-NYM-NYY-2")
    }


def test_new_game_never_steals_a_game_id_claimed_by_another_native_id(storage):
    """Cycle 1 listed only the late game (numbered 1). When the early game shows
    up later, it must get a fresh number, not the already-claimed one."""
    late = make_game_odds(
        game_number=1, start_time=datetime(2026, 7, 9, 23, 5, tzinfo=UTC), native_id="native-late"
    )
    storage.store([late])

    t2 = datetime(2026, 7, 9, 16, 0, tzinfo=UTC)
    early = make_game_odds(
        game_number=1,  # provider numbers by start time within the response
        start_time=datetime(2026, 7, 9, 17, 5, tzinfo=UTC),
        native_id="native-early",
        fetched_at=t2,
    )
    late_again = make_game_odds(
        game_number=2,
        start_time=datetime(2026, 7, 9, 23, 5, tzinfo=UTC),
        native_id="native-late",
        fetched_at=t2,
    )
    storage.store([early, late_again])

    assert late_again.game.game_id == "2026-07-09-NYM-NYY-1"  # keeps its stored id
    assert early.game.game_id == "2026-07-09-NYM-NYY-2"  # bumped past the claimed id
    assert len(storage.games()) == 2


def test_cross_provider_convergence_respects_start_time(storage):
    """Provider X stored both halves of a doubleheader. Provider Y then fetches
    while its feed shows only game 2 (game 1 finished and dropped), so Y's game 2
    arrives provisionally numbered 1. The start-time guard must bump it onto
    game 2's game_id instead of misfiling it under game 1 (D-010)."""
    x1 = make_game_odds(
        provider="prov_x",
        game_number=1,
        start_time=datetime(2026, 7, 9, 17, 20, tzinfo=UTC),
        native_id="X1",
    )
    x2 = make_game_odds(
        provider="prov_x",
        game_number=2,
        start_time=datetime(2026, 7, 9, 23, 5, tzinfo=UTC),
        native_id="X2",
    )
    storage.store([x1, x2])

    y2 = make_game_odds(
        provider="prov_y",
        game_number=1,  # what Y computes with game 2 alone in its feed
        start_time=datetime(2026, 7, 9, 23, 5, tzinfo=UTC),
        native_id="Y2",
        fetched_at=datetime(2026, 7, 9, 21, 0, tzinfo=UTC),
    )
    storage.store([y2])

    assert y2.game.game_id == "2026-07-09-NYM-NYY-2"
    assert [g.game_id for g in storage.games()] == [
        "2026-07-09-NYM-NYY-1",
        "2026-07-09-NYM-NYY-2",
    ]
    # game 1 untouched by Y's cycle: X's start time kept, no Y odds under it
    (game1,) = [g for g in storage.games() if g.game_id.endswith("-1")]
    assert game1.start_time == datetime(2026, 7, 9, 17, 20, tzinfo=UTC)
    assert game1.provider_ids == {"prov_x": "X1"}
    assert all(r[1] == "prov_x" for r in storage.history_rows("2026-07-09-NYM-NYY-1"))
    # ...and the mapping persisted for Y is doubleheader-safe from now on
    (game2,) = [g for g in storage.games() if g.game_id.endswith("-2")]
    assert game2.provider_ids == {"prov_x": "X2", "prov_y": "Y2"}

    # When Y's feed later shows game 1 (say, next morning's backfill numbers it 1
    # again), it converges onto game 1's id — start times agree.
    y1 = make_game_odds(
        provider="prov_y",
        game_number=1,
        start_time=datetime(2026, 7, 9, 17, 20, tzinfo=UTC),
        native_id="Y1",
        fetched_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
    )
    storage.store([y1])
    assert y1.game.game_id == "2026-07-09-NYM-NYY-1"
    assert len(storage.games()) == 2


def test_cross_provider_convergence_tolerates_start_time_drift(storage):
    """The guard must not split one physical game across game_ids when providers
    merely disagree on the start time by minutes (or it moved slightly)."""
    storage.store(
        [make_game_odds(provider="prov_x", native_id="X1")]  # 23:05 default start
    )
    drifted = make_game_odds(
        provider="prov_y",
        native_id="Y1",
        start_time=datetime(2026, 7, 9, 23, 35, tzinfo=UTC),
    )
    storage.store([drifted])

    assert drifted.game.game_id == "2026-07-09-NYM-NYY-1"
    assert len(storage.games()) == 1


def test_latest_odds_keeps_last_known_quotes_per_book(storage):
    """A book missing from the newest cycle keeps its last-known lines
    (ARCHITECTURE: latest snapshot per (game, book, market); SPEC FR1 stores
    partial results as-is)."""

    def ml(book: str, away_price: int, home_price: int) -> list[Quote]:
        return [
            Quote(book=book, market="moneyline", outcome="away", price=away_price),
            Quote(book=book, market="moneyline", outcome="home", price=home_price),
        ]

    t1 = make_game_odds(
        fetched_at=datetime(2026, 7, 9, 15, 0, tzinfo=UTC),
        quotes=ml("draftkings", +120, -140) + ml("fanduel", +125, -145),
    )
    t2 = make_game_odds(
        fetched_at=datetime(2026, 7, 9, 16, 0, tzinfo=UTC),
        quotes=ml("draftkings", +130, -150),  # fanduel dropped out of this cycle
    )
    storage.store([t1])
    storage.store([t2])

    (latest,) = storage.latest_odds()
    assert len(latest.quotes) == 4
    dk_away = next(q for q in latest.quotes if q.book == "draftkings" and q.outcome == "away")
    assert dk_away.price == 130  # newest cycle wins where the book is present
    fd_away = next(q for q in latest.quotes if q.book == "fanduel" and q.outcome == "away")
    assert fd_away.price == 125  # last-known fanduel lines survive
    assert latest.fetched_at == t2.fetched_at
