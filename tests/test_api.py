"""API layer tests. The API is the project's only untrusted-input entry point,
so these lean on the boundary: what a request may not reach, and what the
local-day board must include."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import make_game_odds
from mlb_odds import api
from mlb_odds.client import OddsClient
from mlb_odds.models import Game, GameOdds, Quote
from mlb_odds.storage import Storage


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    db = tmp_path / "odds.sqlite"
    storage = Storage(db)
    storage.store([make_game_odds()])
    storage.close()
    monkeypatch.setenv("MLB_ODDS_DB", str(db))
    return db


@pytest.fixture
def client(seeded_db):
    return TestClient(api.app, raise_server_exceptions=False)


def _local_game(hour_local, away="SFG", home="LAD"):
    """A game starting at `hour_local` today in the machine's local timezone."""
    tz = datetime.now().astimezone().tzinfo
    today = datetime.now(tz).date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=tz).replace(hour=hour_local)
    return GameOdds(
        game=Game(
            game_id=f"{start.astimezone(UTC).date()}-{away}-{home}-1",
            start_time=start,
            home_team=home,
            away_team=away,
            provider_ids={"fake": f"{away}{home}{hour_local}"},
        ),
        fetched_at=datetime.now(UTC),
        provider="fake",
        quotes=[
            Quote(book="dk", market="moneyline", outcome="away", price=120),
            Quote(book="dk", market="moneyline", outcome="home", price=-140),
        ],
    )


# --- the db= parameter must not exist (D-012) -------------------------------


def test_db_query_param_cannot_create_a_file(client, tmp_path):
    evil = tmp_path / "attacker_chosen.sqlite"
    assert client.get(f"/api/today?db={evil}").status_code == 200
    assert not evil.exists()


def test_db_query_param_cannot_redirect_reads(client, tmp_path):
    other = tmp_path / "other.sqlite"
    storage = Storage(other)
    storage.store([_local_game(13, away="BOS", home="TOR")])
    storage.close()

    body = client.get(f"/api/export?fmt=json&db={other}").json()
    assert not any("BOS" in row["game_id"] for row in body["data"])


def test_db_query_param_cannot_migrate_an_unrelated_sqlite_db(client, tmp_path):
    victim = tmp_path / "cookies.sqlite"
    conn = sqlite3.connect(victim)
    conn.execute("CREATE TABLE moz_cookies (id INTEGER)")
    conn.commit()
    conn.close()

    client.get(f"/api/today?db={victim}")

    conn = sqlite3.connect(victim)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert tables == {"moz_cookies"}


# --- read-only storage (D-012) ----------------------------------------------


def test_read_only_storage_never_creates_the_file(tmp_path):
    missing = tmp_path / "absent.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        Storage(missing, read_only=True)
    assert not missing.exists()


def test_read_only_storage_rejects_writes(seeded_db):
    storage = Storage(seeded_db, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        storage.store([make_game_odds()])
    storage.close()


def test_read_only_storage_reads_while_a_writer_holds_the_wal(seeded_db):
    writer = Storage(seeded_db)
    writer._conn.execute("BEGIN IMMEDIATE")
    writer._conn.execute(
        "INSERT INTO games (game_id, start_time, home_team, away_team, season)"
        " VALUES ('held', ?, 'H', 'A', 2026)",
        (datetime.now(UTC).isoformat(),),
    )
    try:
        reader = Storage(seeded_db, read_only=True)
        assert reader.games() is not None
        reader.close()
    finally:
        writer._conn.rollback()
        writer.close()


def test_read_only_client_refuses_providers(seeded_db):
    """The API's inability to spend API credits is a checked invariant (D-012)."""
    with pytest.raises(ValueError):
        OddsClient(providers=[object()], db=seeded_db, read_only=True)


def test_missing_database_is_503_not_500(tmp_path, monkeypatch):
    monkeypatch.setenv("MLB_ODDS_DB", str(tmp_path / "nope.sqlite"))
    assert TestClient(api.app, raise_server_exceptions=False).get(
        "/api/today"
    ).status_code == 503


# --- local-day window (D-013) -----------------------------------------------


def test_board_includes_an_evening_game_that_is_next_day_utc(seeded_db, monkeypatch):
    """A 10pm local first pitch is tomorrow in UTC west of Greenwich. Filtering
    by UTC date would drop it from its own board."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    import time as _time

    _time.tzset()
    try:
        late = _local_game(22)
        assert late.game.start_time.astimezone(UTC).date() != datetime.now(
            datetime.now().astimezone().tzinfo
        ).date(), "fixture precondition: game must fall on a different UTC date"

        storage = Storage(seeded_db)
        storage.store([late])
        storage.close()

        body = TestClient(api.app, raise_server_exceptions=False).get("/api/today").json()
        assert any("LAD" in gb["game"]["game_id"] for gb in body)
    finally:
        monkeypatch.delenv("TZ", raising=False)
        _time.tzset()


def test_board_excludes_games_outside_the_local_day(client, seeded_db):
    storage = Storage(seeded_db)
    storage.store(
        [
            GameOdds(
                game=Game(
                    game_id="9999-01-01-AAA-BBB-1",
                    start_time=datetime.now(UTC) + timedelta(days=30),
                    home_team="BBB",
                    away_team="AAA",
                    provider_ids={"fake": "future"},
                ),
                fetched_at=datetime.now(UTC),
                provider="fake",
                quotes=[Quote(book="dk", market="moneyline", outcome="away", price=120)],
            )
        ]
    )
    storage.close()

    body = client.get("/api/today").json()
    assert not any("AAA" in gb["game"]["game_id"] for gb in body)


def test_window_and_utc_date_disagree_by_design(seeded_db):
    """Guards D-013: window= is an instant range, on_date is a UTC calendar date."""
    storage = Storage(seeded_db, read_only=True)
    start = datetime(2026, 7, 25, 12, tzinfo=UTC)
    assert storage.games(window=(start, start + timedelta(hours=1))) is not None
    with pytest.raises(ValueError):
        storage.games(window=(datetime(2026, 7, 25, 12), start))  # naive bound
    storage.close()


# --- unchanged behaviour that must stay unchanged ---------------------------


def test_history_rejects_unknown_game(client):
    assert client.get("/api/games/does-not-exist/history").status_code == 404


def test_export_rejects_bad_format(client):
    assert client.get("/api/export?fmt=xml").status_code == 400
