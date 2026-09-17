"""Closing-line model, phase 1 (D-046): the nflverse importer, the feature
read over snapshot histories, the training set and its residual scale, the
close-v0 baseline, per-poll prediction recording, and grading that counts
only pre-kickoff predictions. Numbers are computed by hand."""

from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import FIXTURES, make_nfl_spread_odds
from mlb_odds import api, closing, contest
from mlb_odds.cli import app
from mlb_odds.models import ClosePredictionRow, Quote
from mlb_odds.providers.nflverse import parse_games
from mlb_odds.storage import Storage

runner = CliRunner()
KICK = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)
SAMPLE = (FIXTURES / "nflverse_games_sample.csv").read_text()


# --- nflverse ----------------------------------------------------------------------


def test_parse_games_flips_sign_and_maps_codes():
    games = {g.nflverse_id: g for g in parse_games(SAMPLE)}
    phi = games["2025_01_DAL_PHI"]
    assert (phi.away_team, phi.home_team) == ("DAL", "PHI")
    assert phi.spread_line == -8.5  # nflverse +8.5 = home favored by 8.5 -> our -8.5
    assert phi.total_line == 47.5 and (phi.away_score, phi.home_score) == (20, 24)
    assert phi.div_game is True and phi.roof == "outdoors" and (phi.temp, phi.wind) == (75, 11)
    assert (phi.away_qb, phi.home_qb) == ("Dak Prescott", "Jalen Hurts")
    assert games["2016_01_OAK_NO"].away_team == "LV"
    assert games["2016_01_SD_KC"].away_team == "LAC"
    assert games["2019_01_LA_CAR"].away_team == "LAR"
    assert games["1999_01_MIN_ATL"].spread_line == 4.0  # nflverse -4: away favored
    unplayed = games["2026_02_DET_BUF"]
    assert unplayed.home_score is None and unplayed.spread_line == -5.5


def test_history_round_trips_and_upserts(tmp_path):
    storage = Storage(tmp_path / "nfl.sqlite")
    try:
        games = parse_games(SAMPLE)
        assert storage.store_nfl_history(games) == len(games)
        reg = storage.nfl_history()
        assert all(g.game_type == "REG" for g in reg)
        assert [g.nflverse_id for g in storage.nfl_history(seasons=(2025, 2025))] == [
            "2025_01_DAL_PHI",
            "2025_01_DET_GB",
        ]
        updated = games[3].model_copy(update={"home_score": 99})
        storage.store_nfl_history([updated])
        assert (
            next(
                g
                for g in storage.nfl_history(seasons=(2025, 2025))
                if g.nflverse_id == "2025_01_DAL_PHI"
            ).home_score
            == 99
        )
        assert len(storage.nfl_history(game_type=None)) == len(games)
    finally:
        storage.close()


def test_nfl_history_command(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "mlb_odds.providers.nflverse.NFLverse.__init__",
        lambda self, transport=None: _mock_init(self, transport),
    )
    db = tmp_path / "nfl.sqlite"
    result = runner.invoke(app, ["nfl-history", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "7 nflverse game(s) stored (1999-2026)." in result.output
    storage = Storage(db, read_only=True)
    try:
        assert len(storage.nfl_history(game_type=None)) == 7
    finally:
        storage.close()


def _mock_init(self, transport):
    self._client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text=SAMPLE))
    )


# --- snapshot histories -----------------------------------------------------------------


def seed(storage, *, kickoff=KICK):
    """KC @ LAC with a moving line: opener -2.5 a week out, sharp books to -3
    three days out, DK to -3.5 the day before, Pinnacle -3 at close."""
    t0 = kickoff - timedelta(days=7)
    for fetched, lines in (
        (t0, {"draftkings": -2.5, "fanduel": -2.5, "pinnacle": -2.5}),
        (
            kickoff - timedelta(days=3),
            {"draftkings": -2.5, "fanduel": -2.5, "pinnacle": -3.0, "lowvig": -3.0},
        ),
        (
            kickoff - timedelta(days=1),
            {"draftkings": -3.5, "fanduel": -3.0, "pinnacle": -3.0, "lowvig": -3.0},
        ),
        (
            kickoff - timedelta(hours=2),
            {"draftkings": -3.5, "fanduel": -3.0, "pinnacle": -3.0, "lowvig": -3.0},
        ),
    ):
        go = make_nfl_spread_odds(lines, fetched, start_time=kickoff)
        go.quotes.extend(
            [
                Quote(book="draftkings", market="total", outcome="over", line=44.5, price=-110),
                Quote(book="draftkings", market="total", outcome="under", line=44.5, price=-110),
            ]
        )
        storage.store([go])
    return go.game


def test_feature_read_over_the_history(tmp_path):
    storage = Storage(tmp_path / "nfl.sqlite")
    try:
        game = seed(storage)
        ticks = closing.line_history(storage, game.game_id, "spread")
        assert len(ticks) == 15  # 3 + 4 + 4 + 4 book lines
        assert closing.consensus_asof(ticks, KICK - timedelta(days=5)) == -2.5
        assert closing.reference_asof(ticks, KICK - timedelta(days=2)) == ("pinnacle", -3.0)
        assert closing.closing_number(ticks, KICK) == ("pinnacle", -3.0)
        ctx = contest.game_context(storage.games(), game)
        asof = KICK - timedelta(hours=12)
        f = closing.features_asof(ticks, game, asof, ratings_line=-2.0, context=ctx)
    finally:
        storage.close()
    assert f is not None
    assert (f.reference, f.current) == ("pinnacle", -3.0)
    assert f.consensus == -3.0  # median of -3.5, -3, -3, -3
    assert f.opener == -2.5 and f.move_open_to_now == -0.5
    assert f.hours_to_kick == 12.0
    assert f.sharp_gap == 0.25  # sharp median -3.0 minus square median -3.25
    assert f.velocity_24h == -0.25  # consensus 36h out was -2.75
    assert f.ratings_line == -2.0 and f.divisional is True
    # Consensus close when Pinnacle never quoted.
    no_pin = [t for t in ticks if t.book != "pinnacle"]
    assert closing.closing_number(no_pin, KICK) == ("consensus", -3.0)  # median of -3.5, -3, -3


def test_training_rows_and_residual_scale(tmp_path):
    storage = Storage(tmp_path / "nfl.sqlite")
    try:
        seed(storage)
        now = KICK + timedelta(hours=4)
        rows = closing.training_rows(storage, now=now)
    finally:
        storage.close()
    spread = [r for r in rows if r.market == "spread"]
    total = [r for r in rows if r.market == "total"]
    assert len(spread) == 4 and len(total) == 4  # one per snapshot
    assert all(r.close == -3.0 for r in spread)
    # Remaining move from each snapshot's Pinnacle number: -0.5 a week out, then 0.
    assert [r.remaining_move for r in spread] == [-0.5, 0.0, 0.0, 0.0]
    assert [closing.horizon_label(r.features.hours_to_kick) for r in spread] == [
        "168h+",
        "72-168h",
        "24-72h",  # exactly 24h out belongs to the 24-72h bucket
        "0-24h",
    ]
    scale = closing.residual_scale(rows)
    assert scale["spread"]["0-24h"] is None  # one row: no sd
    assert scale["spread"]["168h+"] is None
    assert scale["total"]["24-72h"] is None and scale["total"]["0-24h"] is None
    two = closing.residual_scale(spread + spread)  # duplicated rows: sd defined, zero
    assert two["spread"]["0-24h"] == 0.0
    # Nothing kicked off yet -> empty set.
    storage = Storage(tmp_path / "nfl.sqlite")
    try:
        assert closing.training_rows(storage, now=KICK - timedelta(days=8)) == []
    finally:
        storage.close()


# --- predictions + grading -----------------------------------------------------------


def test_v0_predicts_no_move_and_records_per_poll(tmp_path):
    storage = Storage(tmp_path / "nfl.sqlite")
    try:
        seed(storage)
        now = KICK - timedelta(days=2)
        assert closing.record_predictions(storage, now=now) == 2  # spread + total
        stored = storage.close_predictions()
        assert [
            (p.market, p.reference, p.current, p.predicted_close, p.direction) for p, _k in stored
        ] == [
            ("spread", "pinnacle", -3.0, -3.0, "flat"),
            ("total", "consensus", 44.5, 44.5, "flat"),
        ]
        assert stored[0][0].hours_to_kick == 48.0 and stored[0][0].model_version == "close-v0"
        # Outside the horizon: nothing recorded.
        assert closing.record_predictions(storage, now=KICK - timedelta(days=30)) == 0
    finally:
        storage.close()


def test_report_grades_pre_kickoff_predictions_against_the_close(tmp_path):
    storage = Storage(tmp_path / "nfl.sqlite")
    try:
        game = seed(storage)
        before = KICK - timedelta(days=5)
        storage.store_close_predictions(
            [
                # v0-style: says -2.5 stays; close is -3.0 -> error 0.5, baseline 0.5, no call
                ClosePredictionRow(
                    game_id=game.game_id,
                    market="spread",
                    computed_at=before,
                    hours_to_kick=120.0,
                    reference="pinnacle",
                    current=-2.5,
                    predicted_close=-2.5,
                    sd=None,
                    direction="flat",
                    p_toward=None,
                    model_version="close-v0",
                ),
                # a real call: from -2.5 predicts -3.5 toward home; close -3.0 -> error 0.5, hit
                ClosePredictionRow(
                    game_id=game.game_id,
                    market="spread",
                    computed_at=before,
                    hours_to_kick=120.0,
                    reference="pinnacle",
                    current=-2.5,
                    predicted_close=-3.5,
                    sd=1.0,
                    direction="home",
                    p_toward=0.6,
                    model_version="close-v1",
                ),
                # a wrong call: predicts the line comes back to -2.0 (away) -> miss, error 1.0
                ClosePredictionRow(
                    game_id=game.game_id,
                    market="spread",
                    computed_at=before,
                    hours_to_kick=120.0,
                    reference="pinnacle",
                    current=-2.5,
                    predicted_close=-2.0,
                    sd=1.0,
                    direction="away",
                    p_toward=0.6,
                    model_version="close-v1",
                ),
                # recorded after kickoff: must not count
                ClosePredictionRow(
                    game_id=game.game_id,
                    market="spread",
                    computed_at=KICK + timedelta(hours=1),
                    hours_to_kick=-1.0,
                    reference="pinnacle",
                    current=-3.0,
                    predicted_close=-3.0,
                    sd=None,
                    direction="flat",
                    p_toward=None,
                    model_version="close-v0",
                ),
            ]
        )
        assert closing.report(storage, now=KICK - timedelta(hours=1))["n"] == 0  # not kicked off
        rep = closing.report(storage, now=KICK + timedelta(hours=1))
    finally:
        storage.close()
    assert rep["n"] == 1
    spread = rep["spread"]
    assert spread["n"] == 3
    assert spread["mae"] == round((0.5 + 0.5 + 1.0) / 3, 3)
    assert spread["baseline_mae"] == 0.5
    assert spread["direction_calls"] == 2 and spread["direction_hit_rate"] == 0.5
    assert spread["by_horizon"]["72-168h"]["n"] == 3 and spread["by_horizon"]["0-24h"]["n"] == 0
    assert rep["total"]["n"] == 0 and rep["total"]["mae"] is None


# --- API --------------------------------------------------------------------------------


def test_close_pred_blocks_and_report_endpoint(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "nfl.sqlite")
    try:
        game = seed(storage, kickoff=datetime.now(UTC) + timedelta(days=2))
    finally:
        storage.close()
    monkeypatch.setenv("NFL_ODDS_DB", str(tmp_path / "nfl.sqlite"))
    client = TestClient(api.app, raise_server_exceptions=False)
    body = client.get(f"/api/games/{game.game_id}/markets", params={"sport": "nfl"}).json()
    block = body["close_pred"]
    assert block["spread"]["reference"] == "pinnacle" and block["spread"]["current"] == -3.0
    assert block["spread"]["predicted_close"] == -3.0 and block["spread"]["direction"] == "flat"
    assert 47 < block["spread"]["hours_to_kick"] <= 48
    assert block["total"]["current"] == 44.5 and block["total"]["model_version"] == "close-v0"
    day = game.start_time.astimezone().date().isoformat()
    dash = client.get("/api/dashboard", params={"sport": "nfl", "on": day}).json()
    (g,) = [x for x in dash["games"] if x["game_id"] == game.game_id]
    assert g["close_pred"]["spread"]["predicted_close"] == -3.0
    rep = client.get("/api/model/close/report", params={"sport": "nfl"}).json()
    assert rep["n"] == 0 and rep["model_version"] == "close-v0" and "note" in rep
    assert client.get("/api/model/close/report", params={"sport": "mlb"}).status_code == 422


def test_close_dataset_command(tmp_path):
    storage = Storage(tmp_path / "nfl.sqlite")
    try:
        seed(storage, kickoff=datetime.now(UTC) - timedelta(days=1))
    finally:
        storage.close()
    out = tmp_path / "close.csv"
    result = runner.invoke(
        app, ["close-dataset", "--out", str(out), "--db", str(tmp_path / "nfl.sqlite")]
    )
    assert result.exit_code == 0, result.output
    assert "8 training row(s) written" in result.output
    header, first = out.read_text().splitlines()[:2]
    assert header.startswith("game_id,market,asof,reference,current,consensus,opener")
    assert ",spread," in first and first.endswith(",-0.5")
