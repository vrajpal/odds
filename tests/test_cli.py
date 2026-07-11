"""CLI tests via Typer's CliRunner against a pre-populated temp DB. No network."""

import threading
import time
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from typer.testing import CliRunner

from conftest import FakeProvider, fixture_transport, make_game_odds
from mlb_odds import OddsClient, collector
from mlb_odds.cli import app
from mlb_odds.providers import TheOddsAPI
from mlb_odds.providers.base import ProviderError
from mlb_odds.storage import Storage

runner = CliRunner()


def _today_start_utc(hour_local: int = 19) -> datetime:
    """A start time that falls on today's LOCAL calendar date, expressed in UTC."""
    local = datetime.now().astimezone().replace(
        hour=hour_local, minute=5, second=0, microsecond=0
    )
    return local.astimezone(UTC)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "cli.sqlite"
    storage = Storage(path)
    start = _today_start_utc()
    storage.store(
        [
            make_game_odds(start_time=start, fetched_at=datetime(2026, 7, 9, 15, 0, tzinfo=UTC)),
            make_game_odds(start_time=start, fetched_at=datetime(2026, 7, 9, 16, 0, tzinfo=UTC)),
        ]
    )
    storage.close()
    return path


def _seeded_game_id(db):
    storage = Storage(db)
    (game,) = storage.games()
    storage.close()
    return game.game_id


# ---- today ----


def test_today_renders_board_without_network(db_path):
    result = runner.invoke(app, ["today", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "NYM @ NYY" in result.output
    assert "draftkings" in result.output
    assert "+120/-140" in result.output  # moneyline away/home
    assert "-1.5" in result.output  # run line
    assert "8.5" in result.output  # total


def test_today_excludes_games_on_other_dates(tmp_path):
    """The board must show only today's (local-date) games, not everything stored."""
    db = tmp_path / "mixed.sqlite"
    storage = Storage(db)
    storage.store(
        [
            make_game_odds(start_time=_today_start_utc()),
            make_game_odds(
                away="BOS", home="TB", start_time=_today_start_utc() - timedelta(hours=48)
            ),
        ]
    )
    storage.close()

    result = runner.invoke(app, ["today", "--db", str(db)])

    assert result.exit_code == 0
    assert "NYM @ NYY" in result.output
    assert "BOS @ TB" not in result.output


def test_today_empty_db(tmp_path):
    result = runner.invoke(app, ["today", "--db", str(tmp_path / "empty.sqlite")])

    assert result.exit_code == 0
    assert "No stored odds" in result.output


def test_db_env_var_is_honored(db_path, monkeypatch):
    monkeypatch.setenv("MLB_ODDS_DB", str(db_path))
    result = runner.invoke(app, ["today"])

    assert result.exit_code == 0
    assert "NYM @ NYY" in result.output


def test_dotenv_file_in_cwd_is_loaded(db_path, tmp_path, monkeypatch):
    """A .env in the working directory supplies MLB_ODDS_DB (D-011, cron use case)."""
    monkeypatch.delenv("MLB_ODDS_DB", raising=False)
    cwd = tmp_path / "rundir"
    cwd.mkdir()
    (cwd / ".env").write_text(f"MLB_ODDS_DB={db_path}\n")
    monkeypatch.chdir(cwd)

    result = runner.invoke(app, ["today"])

    assert result.exit_code == 0
    assert "NYM @ NYY" in result.output


def test_real_env_var_beats_dotenv(db_path, tmp_path, monkeypatch):
    """load_dotenv must not override variables already set in the environment."""
    cwd = tmp_path / "rundir"
    cwd.mkdir()
    (cwd / ".env").write_text(f"MLB_ODDS_DB={cwd / 'wrong.sqlite'}\n")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("MLB_ODDS_DB", str(db_path))

    result = runner.invoke(app, ["today"])

    assert result.exit_code == 0
    assert "NYM @ NYY" in result.output  # seeded db, not the .env's empty one


# ---- history ----


def test_history_by_canonical_game_id(db_path):
    game_id = _seeded_game_id(db_path)
    result = runner.invoke(app, ["history", game_id, "--db", str(db_path)])

    assert result.exit_code == 0
    assert "12 rows" in result.output  # 6 quotes x 2 snapshots
    assert "draftkings" in result.output
    assert "moneyline" in result.output


def test_history_fuzzy_away_at_home(db_path):
    result = runner.invoke(app, ["history", "nym@nyy", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "12 rows" in result.output


def test_history_fuzzy_with_date(db_path):
    game_id = _seeded_game_id(db_path)
    utc_date = game_id[:10]
    result = runner.invoke(
        app, ["history", "NYM@NYY", "--date", utc_date, "--db", str(db_path)]
    )

    assert result.exit_code == 0
    assert game_id in result.output


def test_history_fuzzy_ambiguous_doubleheader(tmp_path):
    db = tmp_path / "dh.sqlite"
    storage = Storage(db)
    day = datetime(2026, 7, 9, tzinfo=UTC)
    storage.store(
        [
            make_game_odds(game_number=1, start_time=day.replace(hour=17)),
            make_game_odds(game_number=2, start_time=day.replace(hour=23)),
        ]
    )
    storage.close()

    result = runner.invoke(app, ["history", "NYM@NYY", "--db", str(db)])

    assert result.exit_code == 2
    assert "ambiguous" in result.output
    assert "2026-07-09-NYM-NYY-1" in result.output
    assert "2026-07-09-NYM-NYY-2" in result.output


def test_history_unknown_game(db_path):
    result = runner.invoke(app, ["history", "BOS@TB", "--db", str(db_path)])

    assert result.exit_code == 1
    assert "no stored game" in result.output


# ---- export ----


def test_export_csv(db_path, tmp_path):
    out = tmp_path / "odds.csv"
    result = runner.invoke(
        app, ["export", "--format", "csv", "--out", str(out), "--db", str(db_path)]
    )

    assert result.exit_code == 0
    assert "wrote 12 rows" in result.output
    df = pd.read_csv(out)
    assert len(df) == 12
    assert list(df.columns)[:4] == ["game_id", "start_time", "away_team", "home_team"]


def test_export_parquet(db_path, tmp_path):
    pytest.importorskip("pyarrow")
    out = tmp_path / "odds.parquet"
    result = runner.invoke(
        app, ["export", "--format", "parquet", "--out", str(out), "--db", str(db_path)]
    )

    assert result.exit_code == 0
    df = pd.read_parquet(out)
    assert len(df) == 12


def test_export_rejects_unknown_format(db_path, tmp_path):
    result = runner.invoke(
        app,
        ["export", "--format", "xml", "--out", str(tmp_path / "x"), "--db", str(db_path)],
    )

    assert result.exit_code != 0


# ---- collect ----


def test_collect_once_populates_fresh_db(tmp_path, monkeypatch):
    """The full `collect --once` code path — provider construction, DB resolution,
    collector wiring, close — against a recorded response. No network."""
    db = tmp_path / "collect.sqlite"
    monkeypatch.setenv("THE_ODDS_API_KEY", "test-key")
    monkeypatch.setattr(
        "mlb_odds.cli.TheOddsAPI",
        lambda: TheOddsAPI(api_key="test-key", transport=fixture_transport("normal_day")),
    )

    result = runner.invoke(app, ["collect", "--once", "--db", str(db)])

    assert result.exit_code == 0
    storage = Storage(db)
    assert len(storage.games()) == 2  # the fixture's two games landed
    odds_rows = storage._conn.execute("SELECT COUNT(*) FROM odds").fetchone()[0]
    assert odds_rows > 0
    storage.close()


def test_collect_rejects_nonpositive_interval(tmp_path):
    """--interval 0 would busy-loop against the live API at 3 credits per request."""
    for bad in ("0", "-5"):
        result = runner.invoke(
            app, ["collect", "--interval", bad, "--db", str(tmp_path / "x.sqlite")]
        )
        assert result.exit_code != 0


def test_collect_without_api_key_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    result = runner.invoke(
        app, ["collect", "--once", "--db", str(tmp_path / "x.sqlite")]
    )

    assert result.exit_code == 1
    assert "THE_ODDS_API_KEY" in result.output


# ---- collector (once mode, FakeProvider) ----


def test_collector_once_mode_fetches_and_stores(tmp_path):
    provider = FakeProvider()
    client = OddsClient(providers=[provider], db=tmp_path / "c.sqlite")

    collector.run(client, interval=0.01, once=True)

    assert provider.calls == 1
    assert len(client.current_odds()) == 1
    client.close()


def test_collector_survives_provider_outage(tmp_path):
    broken = FakeProvider(name="broken", error=ProviderError("simulated outage"))
    working = FakeProvider([make_game_odds(provider="working")], name="working")
    client = OddsClient(providers=[broken, working], db=tmp_path / "c.sqlite")

    collector.run(client, once=True)  # must not raise

    assert [go.provider for go in client.current_odds()] == ["working"]
    client.close()


def test_collector_loop_stop_interrupts_sleep(tmp_path):
    """With a long interval, setting the stop event must end the loop promptly."""
    provider = FakeProvider()
    stop = threading.Event()

    def target():
        # SQLite connections are single-thread; build the client where it runs.
        client = OddsClient(providers=[provider], db=tmp_path / "c.sqlite")
        try:
            collector.run(client, interval=60.0, stop=stop)
        finally:
            client.close()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while provider.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)  # let the first cycle finish; loop is now in stop.wait(60)
    stop.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert provider.calls == 1
