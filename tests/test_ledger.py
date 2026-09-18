"""The NFL model accuracy ledger (D-044): snapshots are what the model said
before kickoff; the ledger scores only those, per lens, straight-up and
against the spread. Expectations are computed by hand from chosen inputs."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import FakeProvider, make_nfl_spread_odds
from mlb_odds import api, cli, ledger
from mlb_odds.cli import app
from mlb_odds.models import ModelSnapshot
from mlb_odds.storage import Storage
from test_survivor_matrix import FETCH_AT, WEEK1, WEEK2, build_season

runner = CliRunner()
BEFORE_WEEK1 = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
AFTER_WEEK1 = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def season_db(tmp_path):
    db = tmp_path / "nfl.sqlite"
    build_season(db)
    return db


def test_read_game_uses_pre_kickoff_quotes_only(tmp_path):
    # D-047: the per-game read behind Board/Matrix/snapshots is as-of kickoff
    # on both markets, so a post-kickoff poll's in-play quotes never leak in.
    from conftest import NFL_KICKOFF

    odds = Storage(tmp_path / "nfl.sqlite")
    try:
        pre = make_nfl_spread_odds(
            {"circa": -3.0}, BEFORE_WEEK1, moneylines={"circa": (130, -150)}
        )
        live = make_nfl_spread_odds(
            {"circa": -20.5},
            NFL_KICKOFF + timedelta(minutes=45),
            moneylines={"circa": (2500, -10000)},
        )
        odds.store([pre])
        odds.store([live])
        (game,) = odds.games()
        read = ledger.read_game(odds, game, ledger.fit_market(odds))
    finally:
        odds.close()
    assert read.consensus == -3.0
    assert read.home_wp is not None and 0.55 < read.home_wp < 0.65  # devigged -150, not -10000


# --- snapshots ---------------------------------------------------------------------


def test_snapshots_cover_the_horizon_with_every_lens(season_db):
    odds = Storage(season_db)
    try:
        snaps = ledger.compute_snapshots(odds, now=BEFORE_WEEK1)
        fit = ledger.fit_market(odds)
        games = {g.game_id: g for g in odds.games()}
        for snap in snaps:
            read = ledger.read_game(odds, games[snap.game_id], fit)
            assert snap.market_prob == read.home_wp
            assert snap.model_prob == read.model_wp
            assert snap.consensus_spread == read.consensus
            assert snap.predicted_margin == round(-read.model_line, 1)
    finally:
        odds.close()
    starts = {games[s.game_id].start_time for s in snaps}
    assert starts == {WEEK1, WEEK2}  # WEEK3 (17 days out) and Thanksgiving are beyond the horizon
    assert len(snaps) == 8
    assert all(s.ml_lens_prob is not None and s.spread_lens_prob is not None for s in snaps)
    assert all(s.computed_at == BEFORE_WEEK1 for s in snaps)


def test_ledger_scores_the_latest_pre_kickoff_snapshot_only(season_db):
    odds = Storage(season_db)
    try:
        assert ledger.record_snapshots(odds, now=BEFORE_WEEK1) == 8
        # A second poll after week 1 kicked off: its week-1 rows must not count.
        # Weeks 2 and 3 are now inside the horizon.
        assert ledger.record_snapshots(odds, now=AFTER_WEEK1) == 6
        assert odds.model_outcomes() == []  # nothing graded yet
        week1 = [g for g in odds.games() if g.start_time == WEEK1]
        for g in week1:
            odds.record_result(g.game_id, 24, 17, fetched_at=AFTER_WEEK1)
        outcomes = odds.model_outcomes()
        assert {gid for gid, *_ in outcomes} == {g.game_id for g in week1}
        assert all(snap.computed_at == BEFORE_WEEK1 for _gid, snap, _h, _a in outcomes)
        games, pending, latest = odds.model_snapshot_summary()
        assert (games, pending, latest) == (10, 6, AFTER_WEEK1)
    finally:
        odds.close()


# --- scoring ---------------------------------------------------------------------


def _game(storage, away, home, start):
    go = make_nfl_spread_odds({"circa": -3.0}, FETCH_AT, away=away, home=home, start_time=start)
    storage.store([go])
    return go.game.game_id


def test_accuracy_math_by_hand(tmp_path):
    odds = Storage(tmp_path / "nfl.sqlite")
    try:
        kick = WEEK1
        a = _game(odds, "KC", "LAC", kick)  # home wins by 10; model favored home vs -3
        b = _game(odds, "SF", "SEA", kick)  # away wins by 5; model favored away vs +1
        c = _game(odds, "GB", "CHI", kick)  # home wins by exactly 3 on -3: push
        before = kick - timedelta(hours=6)
        odds.store_model_snapshots(
            [
                ModelSnapshot(
                    game_id=a,
                    computed_at=before,
                    market_prob=0.6,
                    ml_lens_prob=0.7,
                    spread_lens_prob=0.75,
                    model_prob=0.8,
                    predicted_margin=7.0,
                    consensus_spread=-3.0,
                ),
                ModelSnapshot(
                    game_id=b,
                    computed_at=before,
                    market_prob=0.56,
                    ml_lens_prob=0.4,
                    spread_lens_prob=0.45,
                    model_prob=0.3,
                    predicted_margin=-2.0,
                    consensus_spread=1.0,
                ),
                ModelSnapshot(
                    game_id=c,
                    computed_at=before,
                    market_prob=0.5,
                    ml_lens_prob=None,
                    spread_lens_prob=None,
                    model_prob=None,
                    predicted_margin=4.0,
                    consensus_spread=-3.0,
                ),
            ]
        )
        odds.record_result(a, 27, 17, fetched_at=AFTER_WEEK1)
        odds.record_result(b, 20, 25, fetched_at=AFTER_WEEK1)
        odds.record_result(c, 23, 20, fetched_at=AFTER_WEEK1)
        report = ledger.accuracy(odds)
    finally:
        odds.close()
    assert report["n"] == 3
    su = report["straight_up"]
    # model: (0.8-1)^2 + (0.3-0)^2 over 2 scored games
    assert su["model"] == {"n": 2, "brier": 0.065, "hit_rate": 1.0}
    # market: (0.6-1)^2 + (0.56-0)^2 + (0.5-1)^2 over 3; hits: a yes, b no, c yes (0.5 -> home)
    assert su["market"]["n"] == 3
    assert su["market"]["brier"] == round((0.16 + 0.3136 + 0.25) / 3, 4)
    assert su["market"]["hit_rate"] == round(2 / 3, 3)
    assert su["moneyline"]["n"] == 2 and su["spread"]["n"] == 2
    assert report["against_spread"] == {
        "n": 3,
        "model_side_record": "2-0-1",
        "model_side_cover_rate": 1.0,
    }


def test_empty_ledger_is_all_none(tmp_path):
    odds = Storage(tmp_path / "nfl.sqlite")
    try:
        report = ledger.accuracy(odds)
        assert odds.model_snapshot_summary() == (0, 0, None)
    finally:
        odds.close()
    assert report["n"] == 0
    assert report["straight_up"]["model"] == {"n": 0, "brier": None, "hit_rate": None}
    assert report["against_spread"]["model_side_cover_rate"] is None


# --- API + CLI -----------------------------------------------------------------------


def test_model_report_endpoint(season_db, monkeypatch):
    odds = Storage(season_db)
    try:
        ledger.record_snapshots(odds, now=BEFORE_WEEK1)
        for g in odds.games():
            if g.start_time == WEEK1:
                odds.record_result(g.game_id, 21, 20, fetched_at=AFTER_WEEK1)
    finally:
        odds.close()
    monkeypatch.setenv("NFL_ODDS_DB", str(season_db))
    client = TestClient(api.app, raise_server_exceptions=False)
    body = client.get("/api/model/report", params={"sport": "nfl"}).json()
    assert body["sport"] == "nfl" and body["n"] == 4
    assert body["snapshots"] == {
        "games": 8,
        "awaiting_result": 4,
        "latest": BEFORE_WEEK1.isoformat(),
    }
    assert set(body["straight_up"]) == {"market", "moneyline", "spread", "model"}
    assert body["straight_up"]["model"]["n"] == 4
    assert client.get("/api/model/report", params={"sport": "mlb"}).status_code == 422


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return BEFORE_WEEK1 if tz else BEFORE_WEEK1.replace(tzinfo=None)


def test_model_snapshot_command(season_db, monkeypatch):
    monkeypatch.setattr(cli, "datetime", _Frozen)
    result = runner.invoke(app, ["model-snapshot", "--sport", "nfl", "--db", str(season_db)])
    assert result.exit_code == 0, result.output
    assert "8 model snapshot(s) recorded." in result.output
    assert (
        runner.invoke(app, ["model-snapshot", "--sport", "mlb", "--db", str(season_db)]).exit_code
        == 2
    )


def test_nfl_collect_records_a_snapshot_after_the_poll(tmp_path, monkeypatch):
    db = tmp_path / "nfl.sqlite"
    odds_row = make_nfl_spread_odds({"circa": -3.0}, BEFORE_WEEK1, start_time=WEEK1)
    monkeypatch.setattr(cli, "_build_providers", lambda *a, **k: [FakeProvider([odds_row])])
    monkeypatch.setattr(cli, "datetime", _Frozen)
    result = runner.invoke(app, ["collect", "--once", "--sport", "nfl", "--db", str(db)])
    assert result.exit_code == 0, result.output
    storage = Storage(db, read_only=True)
    try:
        games, pending, latest = storage.model_snapshot_summary()
        rows = storage._conn.execute(
            "SELECT market_prob, model_prob FROM model_snapshots"
        ).fetchall()
    finally:
        storage.close()
    assert (games, pending, latest) == (1, 1, BEFORE_WEEK1)
    ((market, blend),) = rows
    assert market is not None and blend is None  # one lined game: no fits, market only
