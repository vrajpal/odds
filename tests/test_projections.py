"""Projection import tests (D-037). Inline CSVs are representative game-level
shapes; fixtures/fanduel_research_mlb_daily.csv is an unedited real FanDuel
Research MLB export (player-level DFS projections), which is the shape the
site actually ships — the parser aggregates it to per-game run projections."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import make_game_odds, make_nfl_spread_odds
from mlb_odds.cli import app
from mlb_odds.projections import (
    ProjectionParseError,
    brier,
    parse_csv,
    projection_prob,
    resolve_team,
)
from mlb_odds.storage import Storage

runner = CliRunner()


# --- team resolution ---


def test_resolve_team_forms():
    assert resolve_team("mlb", "New York Yankees") == "NYY"
    assert resolve_team("mlb", "NYY") == "NYY"
    assert resolve_team("mlb", "Yankees") == "NYY"
    assert resolve_team("nfl", "Chiefs") == "KC"
    assert resolve_team("mlb", "Sox") is None  # ambiguous suffix (Red/White)
    assert resolve_team("mlb", "Narwhals") is None
    # FanDuel abbreviations that differ from our canonical codes.
    assert resolve_team("mlb", "CHW") == "CWS"
    assert resolve_team("mlb", "OAK") == "ATH"
    assert resolve_team("nfl", "JAC") == "JAX"


# --- parsing ---


def test_parse_win_prob_csv_with_percent_signs():
    csv_text = (
        "Away Team,Home Team,Home Win%\n"
        "Boston Red Sox,New York Yankees,58.5%\n"
        "Cubs,Cardinals,44%\n"
    )
    rows = parse_csv(csv_text, "mlb")
    assert rows[0].home_win_prob == pytest.approx(0.585)
    assert (rows[1].away_team, rows[1].home_team) == ("CHC", "STL")


def test_parse_projected_scores_and_away_prob_fallback():
    csv_text = (
        "away,home,away score,home score,Away Win %\n"
        "Chiefs,Bills,24.5,27.1,0.42\n"
    )
    (row,) = parse_csv(csv_text, "nfl")
    assert row.home_win_prob == pytest.approx(0.58)
    assert row.home_score == 27.1
    # Score-derived prob when the stated prob is absent.
    assert projection_prob(None, 24.5, 27.1, "nfl") > 0.5


def test_parse_skips_bad_rows_but_fails_on_unmappable_headers():
    rows = parse_csv(
        "away,home,home win%\nYankees,Red Sox,45%\nTBD,TBD,50%\n", "mlb"
    )
    assert len(rows) == 1  # TBD row skipped, import survives
    with pytest.raises(ProjectionParseError, match="couldn't map"):
        parse_csv("colA,colB\nx,y\n", "mlb")
    with pytest.raises(ProjectionParseError, match="no rows"):
        parse_csv("away,home,home win%\nTBD,TBD,50%\n", "mlb")


# --- player-level exports (the real FanDuel Research shape) ---


def test_parse_real_fanduel_player_export():
    text = (Path(__file__).parent / "fixtures"
            / "fanduel_research_mlb_daily.csv").read_text()
    rows = parse_csv(text, "mlb")
    assert len(rows) == 10  # ten games on the slate
    by_game = {(r.away_team, r.home_team): r for r in rows}
    assert ("CLE", "CWS") in by_game  # gameInfo said CHW
    assert ("ATH", "BOS") in by_game  # gameInfo said OAK
    row = by_game[("COL", "STL")]
    assert row.home_win_prob is None  # player exports carry no win prob
    # Lineup run sums normalized to a 38-PA game land in a sane MLB range,
    # and the derived probability stays near a coin flip.
    assert row.away_score is not None and 3.0 < row.away_score < 6.0
    assert row.home_score is not None and 3.0 < row.home_score < 6.0
    p = projection_prob(None, row.away_score, row.home_score, "mlb")
    assert p is not None and 0.4 < p < 0.6


def test_player_aggregation_normalizes_lineup_length():
    # Same per-PA scoring rate on both sides; the home side lists fewer
    # hitters. Normalization must equalize the projected scores.
    header = "player,team,runs,plateAppearances,gameInfo\n"
    away = [f"A{i},New York Yankees,0.5,4.0,NYY @ BOS" for i in range(9)]
    home = [f"H{i},Boston Red Sox,0.5,4.0,NYY @ BOS" for i in range(7)]
    (row,) = parse_csv(header + "\n".join(away + home) + "\n", "mlb")
    assert row.away_score == row.home_score
    # And too-thin listings don't produce a game at all.
    thin = [f"H{i},Boston Red Sox,0.5,4.0,NYY @ BOS" for i in range(3)]
    with pytest.raises(ProjectionParseError, match="no aggregatable"):
        parse_csv(header + "\n".join(away + thin) + "\n", "mlb")


def test_player_export_rejected_for_nfl():
    text = "player,team,runs,gameInfo\nJosh Allen,Buffalo Bills,2,BUF @ KC\n"
    with pytest.raises(ProjectionParseError, match="only aggregatable for mlb"):
        parse_csv(text, "nfl")


# --- storage: append-only history ---


def test_projection_history_appends_and_latest_wins(tmp_path):
    storage = Storage(tmp_path / "odds.sqlite")
    go = make_game_odds()
    storage.store([go])
    gid = go.game.game_id
    t0 = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    storage.add_projection(gid, "fanduel_research", fetched_at=t0,
                           home_win_prob=0.55, away_score=None, home_score=None)
    storage.add_projection(gid, "fanduel_research", fetched_at=t0 + timedelta(hours=20),
                           home_win_prob=0.60, away_score=None, home_score=None)
    latest = storage.latest_projection(gid)
    assert latest is not None and latest[2] == 0.60
    # Both snapshots retained — the history is the ledger.
    count = storage._conn.execute(
        "SELECT COUNT(*) FROM projections WHERE game_id = ?", (gid,)
    ).fetchone()[0]
    assert count == 2
    storage.close()


def test_projection_outcomes_use_last_prekickoff_snapshot(tmp_path):
    storage = Storage(tmp_path / "odds.sqlite")
    start = datetime(2026, 7, 9, 23, 5, tzinfo=UTC)
    go = make_game_odds(start_time=start)
    storage.store([go])
    gid = go.game.game_id
    storage.add_projection(gid, "s", fetched_at=start - timedelta(days=1),
                           home_win_prob=0.52, away_score=None, home_score=None)
    storage.add_projection(gid, "s", fetched_at=start - timedelta(hours=1),
                           home_win_prob=0.61, away_score=None, home_score=None)
    # A post-kickoff snapshot must never count (hindsight is not forecasting).
    storage.add_projection(gid, "s", fetched_at=start + timedelta(hours=1),
                           home_win_prob=0.99, away_score=None, home_score=None)
    storage.record_result(gid, 5, 3, fetched_at=start + timedelta(hours=4))
    rows = storage.projection_outcomes()
    assert len(rows) == 1
    assert rows[0][1] == 0.61  # the last pre-kickoff opinion
    storage.close()


def test_brier_math():
    assert brier([(0.5, 1), (0.5, 0)]) == 0.25  # coin flip
    assert brier([(1.0, 1), (0.0, 0)]) == 0.0  # oracle
    assert brier([]) is None


# --- CLI import end to end ---


def test_projections_cli_import(tmp_path):
    db = tmp_path / "nfl.sqlite"
    storage = Storage(db)
    future = datetime.now(UTC) + timedelta(days=3)
    go = make_nfl_spread_odds({"circa": -3.0}, datetime.now(UTC),
                              away="BUF", home="KC", start_time=future)
    storage.store([go])
    storage.close()

    csv_path = tmp_path / "proj.csv"
    csv_path.write_text("away,home,home win%\nBills,Chiefs,61%\nJets,Dolphins,44%\n")
    result = runner.invoke(
        app, ["projections", str(csv_path), "--sport", "nfl", "--db", str(db)]
    )
    assert result.exit_code == 0, result.output
    assert "1/2 projections matched" in result.output  # MIA game not stored
    assert "skipped NYJ @ MIA: no stored game" in result.output

    storage = Storage(db)
    latest = storage.latest_projection(go.game.game_id)
    assert latest is not None
    assert latest[0] == "fanduel_research" and latest[2] == pytest.approx(0.61)
    storage.close()


def test_projections_cli_requires_csv_xor_fetch(tmp_path):
    result = runner.invoke(app, ["projections"])
    assert result.exit_code == 2
    csv_path = tmp_path / "p.csv"
    csv_path.write_text("away,home,home win%\n")
    result = runner.invoke(app, ["projections", str(csv_path), "--fetch"])
    assert result.exit_code == 2
    result = runner.invoke(app, ["projections", "--fetch", "--sport", "nfl"])
    assert result.exit_code == 2  # live fetch is MLB-only
