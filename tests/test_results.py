"""Results collector tests (D-024): storage roundtrip and migration, client
matching against a real recorded completed slate, CLI end-to-end."""

import json
from datetime import UTC, date, datetime, timedelta

import httpx
from typer.testing import CliRunner

from conftest import FIXTURES, make_game_odds, make_nfl_spread_odds
from mlb_odds.cli import app
from mlb_odds.client import OddsClient
from mlb_odds.providers.base import ProviderError
from mlb_odds.providers.espn import ESPN
from mlb_odds.storage import Storage

runner = CliRunner()

# The real recorded slate (2026-08-05): TOR 5 @ HOU 4, LAD 6 @ CHC 7.
TOR_HOU_START = datetime(2026, 8, 5, 18, 10, tzinfo=UTC)
LAD_CHC_START = datetime(2026, 8, 5, 18, 20, tzinfo=UTC)


def scoreboard_transport(fixture: str) -> httpx.MockTransport:
    payload = json.loads((FIXTURES / f"{fixture}.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def seed_mlb_slate(db) -> dict[str, str]:
    storage = Storage(db)
    ids = {}
    for away, home, start in [("TOR", "HOU", TOR_HOU_START), ("LAD", "CHC", LAD_CHC_START)]:
        go = make_game_odds(away=away, home=home, start_time=start)
        storage.store([go])
        ids[f"{away}@{home}"] = go.game.game_id
    storage.close()
    return ids


# --- storage ---


def test_results_roundtrip_and_correction(tmp_path):
    storage = Storage(tmp_path / "odds.sqlite")
    go = make_game_odds()
    storage.store([go])
    gid = go.game.game_id
    assert storage.result(gid) is None

    now = datetime.now(UTC)
    storage.record_result(gid, 4, 5, fetched_at=now)
    assert storage.result(gid) == (4, 5)
    # Stat correction overwrites.
    storage.record_result(gid, 3, 5, fetched_at=now + timedelta(hours=1))
    assert storage.result(gid) == (3, 5)
    storage.close()


def test_games_missing_results_work_list(tmp_path):
    storage = Storage(tmp_path / "odds.sqlite")
    past = make_game_odds(away="TOR", home="HOU", start_time=TOR_HOU_START)
    future = make_game_odds(
        away="LAD", home="CHC", start_time=datetime.now(UTC) + timedelta(days=2)
    )
    storage.store([past])
    storage.store([future])

    cutoff = datetime.now(UTC)
    missing = storage.games_missing_results(before=cutoff)
    assert [g.game_id for g in missing] == [past.game.game_id]  # future game excluded

    storage.record_result(past.game.game_id, 4, 5, fetched_at=cutoff)
    assert storage.games_missing_results(before=cutoff) == []
    storage.close()


def test_migration_adds_results_to_existing_v3_database(tmp_path):
    # Simulate a pre-results database: create current schema, drop the table
    # and wind the version back — reopening must re-apply migration 4 only.
    db = tmp_path / "odds.sqlite"
    storage = Storage(db)
    storage.store([make_game_odds()])
    storage._conn.executescript(
        "DROP TABLE results; DROP TABLE probables; DROP TABLE statcast_team;"
        " DROP TABLE statcast_pitcher; DROP TABLE projections;"
        " DELETE FROM schema_version;"
        " INSERT INTO schema_version (version) VALUES (3);"
    )
    storage._conn.commit()
    storage.close()

    reopened = Storage(db)  # migrates
    reopened.record_result("2026-07-09-NYM-NYY-1", 2, 1, fetched_at=datetime.now(UTC))
    assert reopened.result("2026-07-09-NYM-NYY-1") == (2, 1)
    assert len(reopened.games(date(2026, 7, 9))) == 1  # old data intact
    reopened.close()


# --- client matching against the real recorded slate ---


def test_fetch_and_store_results_records_real_slate(tmp_path):
    db = tmp_path / "odds.sqlite"
    ids = seed_mlb_slate(db)
    client = OddsClient(providers=[], db=db)
    source = ESPN(sport="mlb", transport=scoreboard_transport("espn_mlb_scoreboard_final"))

    recorded = client.fetch_and_store_results(source, [date(2026, 8, 5)])
    assert recorded == 2
    assert client.result(ids["TOR@HOU"]) == (4, 5)  # home HOU 4, away TOR 5
    assert client.result(ids["LAD@CHC"]) == (7, 6)
    assert client.games_missing_results(before=datetime.now(UTC)) == []
    # Idempotent re-run: nothing left to record.
    assert client.fetch_and_store_results(source, [date(2026, 8, 5)]) == 0
    client.close()


def test_matching_respects_start_time_tolerance(tmp_path):
    # Same matchup stored 6h from the fixture's start time (a different
    # physical game, doubleheader-style): must NOT receive this score.
    db = tmp_path / "odds.sqlite"
    storage = Storage(db)
    other_half = make_game_odds(
        away="TOR", home="HOU", start_time=TOR_HOU_START + timedelta(hours=6)
    )
    storage.store([other_half])
    storage.close()

    client = OddsClient(providers=[], db=db)
    source = ESPN(sport="mlb", transport=scoreboard_transport("espn_mlb_scoreboard_final"))
    assert client.fetch_and_store_results(source, [date(2026, 8, 5)]) == 0
    assert client.result(other_half.game.game_id) is None
    client.close()


def test_in_progress_games_are_not_recorded(tmp_path):
    db = tmp_path / "nfl-odds.sqlite"
    storage = Storage(db)
    live = make_nfl_spread_odds(
        {"circa": -3.0},
        datetime(2026, 8, 1, tzinfo=UTC),
        away="SF",
        home="SEA",
        start_time=datetime(2026, 9, 13, 20, 25, tzinfo=UTC),
    )
    done = make_nfl_spread_odds(
        {"circa": -3.0},
        datetime(2026, 8, 1, tzinfo=UTC),
        away="KC",
        home="LAC",
        start_time=datetime(2026, 9, 13, 17, 0, tzinfo=UTC),
    )
    storage.store([live])
    storage.store([done])
    storage.close()

    client = OddsClient(providers=[], db=db)
    source = ESPN(
        sport="nfl", transport=scoreboard_transport("espn_nfl_scoreboard_finals_week1")
    )
    assert client.fetch_and_store_results(
        source, [date(2026, 9, 13)], before=datetime(2026, 9, 14, tzinfo=UTC)
    ) == 1
    assert client.result(done.game.game_id) == (27, 20)
    assert client.result(live.game.game_id) is None  # in progress: wait
    client.close()


def test_failed_day_is_logged_not_fatal(tmp_path):
    class FailingSource:
        def fetch_final_scores(self, on):
            raise ProviderError("scoreboard down")

    client = OddsClient(providers=[], db=tmp_path / "odds.sqlite")
    assert client.fetch_and_store_results(FailingSource(), [date(2026, 8, 5)]) == 0
    client.close()


# --- CLI ---


def test_results_command_default_day_derivation(tmp_path, monkeypatch):
    db = tmp_path / "odds.sqlite"
    ids = seed_mlb_slate(db)
    monkeypatch.setattr(
        "mlb_odds.cli.ESPN",
        lambda sport="mlb": ESPN(
            sport=sport, transport=scoreboard_transport("espn_mlb_scoreboard_final")
        ),
    )
    result = runner.invoke(app, ["results", "--db", str(db)])
    assert result.exit_code == 0
    assert "2 final score(s) recorded; 0 still missing." in result.output

    storage = Storage(db)
    assert storage.result(ids["LAD@CHC"]) == (7, 6)
    storage.close()

    # Second run: nothing pending, no network needed.
    again = runner.invoke(app, ["results", "--db", str(db)])
    assert "No completed games are missing scores." in again.output


def test_results_command_explicit_date(tmp_path, monkeypatch):
    db = tmp_path / "odds.sqlite"
    seed_mlb_slate(db)
    monkeypatch.setattr(
        "mlb_odds.cli.ESPN",
        lambda sport="mlb": ESPN(
            sport=sport, transport=scoreboard_transport("espn_mlb_scoreboard_final")
        ),
    )
    result = runner.invoke(app, ["results", "--date", "2026-08-05", "--db", str(db)])
    assert result.exit_code == 0
    assert "2 final score(s) recorded" in result.output
