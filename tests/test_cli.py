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
from mlb_odds.models import Quote
from mlb_odds.providers import ESPN, TheOddsAPI
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
    # chdir away from the repo root: the CLI loads .env from cwd (D-011), so a
    # developer's real key would otherwise reach the live API — a 3-credit poll
    # per test run.
    monkeypatch.chdir(tmp_path)
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


# ---- closing ----


def test_closing_renders_pre_start_board_only(tmp_path):
    db = tmp_path / "closing.sqlite"
    start = _today_start_utc()
    storage = Storage(db)
    storage.store(
        [
            make_game_odds(start_time=start, fetched_at=start - timedelta(hours=2)),
            make_game_odds(
                start_time=start,
                fetched_at=start + timedelta(minutes=30),
                quotes=[Quote(book="draftkings", market="moneyline", outcome="away", price=999)],
            ),
        ]
    )
    storage.close()

    result = runner.invoke(app, ["closing", "--db", str(db)])

    assert result.exit_code == 0
    assert "NYM @ NYY" in result.output
    assert "+120/-140" in result.output  # pre-start snapshot
    assert "+999" not in result.output  # in-game snapshot is not a closing line


def test_closing_date_filter_and_empty_message(tmp_path):
    db = tmp_path / "closing.sqlite"
    start = datetime(2026, 7, 9, 23, 5, tzinfo=UTC)
    storage = Storage(db)
    storage.store([make_game_odds(start_time=start, fetched_at=start - timedelta(hours=1))])
    storage.close()

    hit = runner.invoke(app, ["closing", "--date", "2026-07-09", "--db", str(db)])
    assert hit.exit_code == 0
    assert "NYM @ NYY" in hit.output

    miss = runner.invoke(app, ["closing", "--date", "2026-07-11", "--db", str(db)])
    assert miss.exit_code == 0
    assert "No closing lines stored for 2026-07-11" in miss.output


# ---- changed_only wiring (D-015) ----


def test_collect_changed_only_skips_unchanged_cycles(tmp_path, monkeypatch):
    """Two --changed-only cycles with an identical board: the second appends 0
    rows. Exercises the full CLI -> client -> storage wiring."""
    monkeypatch.setenv("THE_ODDS_API_KEY", "test-key")
    monkeypatch.setattr(
        "mlb_odds.cli.TheOddsAPI",
        lambda: TheOddsAPI(api_key="test-key", transport=fixture_transport("normal_day")),
    )
    db = tmp_path / "collect.sqlite"

    for _ in range(2):
        result = runner.invoke(app, ["collect", "--once", "--changed-only", "--db", str(db)])
        assert result.exit_code == 0

    storage = Storage(db)
    fetches = storage._conn.execute("SELECT COUNT(DISTINCT fetched_at) FROM odds").fetchone()[0]
    storage.close()
    assert fetches == 1  # second cycle contributed nothing


# ---- rigor backlog: FR6 quota logging ----


def test_collector_cycle_summary_reaches_the_log(tmp_path, caplog):
    """FR6: the quota number must actually land in the log record, not just be
    computed — deleting the logger.info call should fail this test."""
    provider = FakeProvider(quota_remaining=42)
    client = OddsClient(providers=[provider], db=tmp_path / "c.sqlite")

    with caplog.at_level("INFO", logger="mlb_odds.collector"):
        collector.run(client, once=True)
    client.close()

    cycle_lines = [r.getMessage() for r in caplog.records if "cycle:" in r.getMessage()]
    assert len(cycle_lines) == 1
    assert "quota remaining: fake=42" in cycle_lines[0]
    assert "1 games" in cycle_lines[0]


# ---- rigor backlog: SIGINT handler swap/restore ----


def test_sigint_handler_swapped_during_run_and_restored_after(tmp_path):
    import signal

    seen: list[object] = []

    class ProbingProvider(FakeProvider):
        def fetch_game_lines(self):
            seen.append(signal.getsignal(signal.SIGINT))
            return super().fetch_game_lines()

    original = signal.getsignal(signal.SIGINT)
    client = OddsClient(providers=[ProbingProvider()], db=tmp_path / "c.sqlite")
    collector.run(client, once=True)
    client.close()

    assert len(seen) == 1
    assert seen[0] is not original, "collector must install its own SIGINT handler"
    assert signal.getsignal(signal.SIGINT) is original, "handler must be restored"


# ---- rigor backlog: collect loop-mode wiring ----


@pytest.mark.parametrize(
    ("argv", "expected_once"),
    [(["collect", "--once"], True), (["collect", "--interval", "77"], False)],
)
def test_collect_wires_once_and_interval_through(tmp_path, monkeypatch, argv, expected_once):
    """Fails if the CLI hardcodes once=True (or drops --interval) when calling
    collector.run."""
    monkeypatch.setattr("mlb_odds.cli.TheOddsAPI", lambda: FakeProvider())
    calls: list[tuple[float, bool]] = []
    monkeypatch.setattr(
        "mlb_odds.cli.collector.run",
        lambda client, interval, *, once=False, live=False, stop=None: calls.append(
            (interval, once)
        ),
    )

    result = runner.invoke(app, [*argv, "--db", str(tmp_path / "x.sqlite")])

    assert result.exit_code == 0
    expected_interval = 77.0 if not expected_once else 300.0
    assert calls == [(expected_interval, expected_once)]


# ---- rigor backlog: local-timezone display ----


@pytest.fixture
def new_york_tz(monkeypatch):
    """Pin the process to America/New_York so rendered local times are stable."""
    import time as _time

    monkeypatch.setenv("TZ", "America/New_York")
    _time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    _time.tzset()


def test_today_renders_local_time_for_game_crossing_utc_midnight(tmp_path, new_york_tz):
    """A 10:05pm ET start is 02:05 UTC the NEXT day. The board must still show
    it today, rendered in local time — pinning both the local-date filter and
    the display conversion."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    local_start = datetime.now(tz).replace(hour=22, minute=5, second=0, microsecond=0)
    assert local_start.astimezone(UTC).date() != local_start.date()  # crosses UTC midnight

    db = tmp_path / "tz.sqlite"
    storage = Storage(db)
    storage.store([make_game_odds(start_time=local_start.astimezone(UTC))])
    storage.close()

    result = runner.invoke(app, ["today", "--db", str(db)])

    assert result.exit_code == 0
    assert "NYM @ NYY" in result.output
    assert f"{local_start:%Y-%m-%d} 10:05 PM" in result.output
    assert ("EDT" in result.output) or ("EST" in result.output)


def test_history_renders_fetched_at_in_local_time(tmp_path, new_york_tz):
    """fetched_at is stored UTC; the history table must render it converted
    to the local zone (D-011: local time exists only at the display layer)."""
    fetched = datetime(2026, 7, 9, 18, 30, tzinfo=UTC)  # 14:30 EDT

    db = tmp_path / "tz.sqlite"
    storage = Storage(db)
    game = make_game_odds(fetched_at=fetched)
    storage.store([game])
    storage.close()

    result = runner.invoke(app, ["history", game.game.game_id, "--db", str(db)])

    assert result.exit_code == 0
    assert "14:30:00" in result.output
    assert "18:30:00" not in result.output


# ---- live mode (D-017) ----


def test_seconds_until_live_window_states():
    from mlb_odds.collector import LIVE_LEAD, LIVE_TAIL, seconds_until_live

    now = datetime(2026, 7, 9, 20, 0, tzinfo=UTC)

    def game_at(start):
        return make_game_odds(start_time=start).game

    # in window: started an hour ago
    assert seconds_until_live([game_at(now - timedelta(hours=1))], now) == 0.0
    # in window: first pitch in 10 minutes (inside the 15m lead)
    assert seconds_until_live([game_at(now + timedelta(minutes=10))], now) == 0.0
    # upcoming: opens lead-minutes before start
    wait = seconds_until_live([game_at(now + timedelta(hours=2))], now)
    assert wait == (timedelta(hours=2) - LIVE_LEAD).total_seconds()
    # over: past the tail
    assert seconds_until_live([game_at(now - LIVE_TAIL)], now) is None
    # empty slate
    assert seconds_until_live([], now) is None
    # nearest upcoming wins
    soon = game_at(now + timedelta(hours=1))
    later = game_at(now + timedelta(hours=6))
    assert seconds_until_live([later, soon], now) == (
        timedelta(hours=1) - LIVE_LEAD
    ).total_seconds()


def test_live_mode_polls_during_a_live_game(tmp_path):
    db = tmp_path / "live.sqlite"
    storage = Storage(db)
    storage.store([make_game_odds(start_time=datetime.now(UTC) - timedelta(minutes=30))])
    storage.close()

    provider = FakeProvider()
    client = OddsClient(providers=[provider], db=db)
    collector.run(client, once=True, live=True)
    client.close()

    assert provider.calls == 1


def test_live_mode_idles_when_no_game_is_live(tmp_path):
    """A game 6h out: the loop must wait, not poll (that's the whole point —
    no wasted credits between windows)."""
    db = tmp_path / "idle.sqlite"
    storage = Storage(db)
    storage.store([make_game_odds(start_time=datetime.now(UTC) + timedelta(hours=6))])
    storage.close()

    provider = FakeProvider()
    stop = threading.Event()

    def target():
        client = OddsClient(providers=[provider], db=db)
        try:
            collector.run(client, interval=0.01, live=True, stop=stop)
        finally:
            client.close()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    time.sleep(0.3)  # long enough for several 0.01s intervals if it were polling
    stop.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert provider.calls == 0


def test_collect_rejects_once_with_live(tmp_path, monkeypatch):
    monkeypatch.setenv("THE_ODDS_API_KEY", "test-key")
    result = runner.invoke(
        app, ["collect", "--once", "--live", "--db", str(tmp_path / "x.sqlite")]
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_collect_wires_live_through(tmp_path, monkeypatch):
    monkeypatch.setattr("mlb_odds.cli.TheOddsAPI", lambda: FakeProvider())
    calls = []
    monkeypatch.setattr(
        "mlb_odds.cli.collector.run",
        lambda client, interval, *, once=False, live=False, stop=None: calls.append(live),
    )
    result = runner.invoke(app, ["collect", "--live", "--db", str(tmp_path / "x.sqlite")])
    assert result.exit_code == 0
    assert calls == [True]
# ---- provider selection ----


def test_collect_provider_espn_needs_no_key(tmp_path, monkeypatch):
    """--provider espn must work with no THE_ODDS_API_KEY anywhere (free source)."""
    monkeypatch.chdir(tmp_path)  # keep the repo-root .env out of reach (D-011)
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    monkeypatch.setattr(
        "mlb_odds.cli.ESPN",
        lambda: ESPN(transport=fixture_transport("espn_scoreboard_normal")),
    )

    result = runner.invoke(
        app, ["collect", "--once", "--provider", "espn", "--db", str(tmp_path / "e.sqlite")]
    )

    assert result.exit_code == 0
    storage = Storage(tmp_path / "e.sqlite")
    assert len(storage.games()) == 2
    storage.close()


def test_collect_provider_all_uses_both(tmp_path, monkeypatch):
    monkeypatch.setenv("THE_ODDS_API_KEY", "test-key")
    monkeypatch.setattr(
        "mlb_odds.cli.TheOddsAPI",
        lambda: TheOddsAPI(api_key="test-key", transport=fixture_transport("normal_day")),
    )
    monkeypatch.setattr(
        "mlb_odds.cli.ESPN",
        lambda: ESPN(transport=fixture_transport("espn_scoreboard_normal")),
    )

    result = runner.invoke(
        app, ["collect", "--once", "--provider", "all", "--db", str(tmp_path / "b.sqlite")]
    )

    assert result.exit_code == 0
    storage = Storage(tmp_path / "b.sqlite")
    providers = {
        row[0] for row in storage._conn.execute("SELECT DISTINCT provider FROM odds")
    }
    storage.close()
    assert providers == {"the_odds_api", "espn"}


def test_collect_default_provider_unchanged(tmp_path, monkeypatch):
    """No --provider flag: exactly the pre-flag behavior (The Odds API only)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    result = runner.invoke(app, ["collect", "--once", "--db", str(tmp_path / "x.sqlite")])
    assert result.exit_code == 1
    assert "THE_ODDS_API_KEY" in result.output
