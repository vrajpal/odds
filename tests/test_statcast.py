"""Statcast scouting tests (D-031). statsapi schedule and Savant team CSV are
real recordings (2026-08-09, trimmed); the pitcher CSV is a trimmed recording;
the ESPN 2026-08-08 scoreboard is an edited fixture mirroring the statsapi one
so the CLI exercises schedule-store + probable-matching offline."""

import json
from datetime import UTC, date, datetime

import httpx
from typer.testing import CliRunner

from conftest import FIXTURES, make_game_odds
from mlb_odds.cli import app
from mlb_odds.statcast import StatcastSource
from mlb_odds.storage import Storage

runner = CliRunner()


def statcast_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "statsapi.mlb.com":
            return httpx.Response(
                200, json=json.loads((FIXTURES / "statsapi_schedule.json").read_text())
            )
        if host == "baseballsavant.mlb.com":
            kind = request.url.params.get("type")
            name = (
                "savant_team_expected.csv"
                if kind == "batter-team"
                else "savant_pitcher_expected.csv"
            )
            return httpx.Response(200, text=(FIXTURES / name).read_text())
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def source() -> StatcastSource:
    return StatcastSource(transport=statcast_transport())


def test_probables_canonicalized_with_starters():
    games = source().probables(date(2026, 8, 8))
    assert len(games) == 4
    atl = next(g for g in games if g.away_team == "ATL")
    assert atl.home_team == "NYY"
    assert (atl.away_pitcher, atl.home_pitcher) == ("Chris Sale", "Gerrit Cole")
    # "Athletics" (no city since 2025) normalizes like every other name.
    assert any(g.away_team == "ATH" for g in games)


def test_team_expected_maps_savant_abbrevs():
    rows = source().team_expected(2026)
    assert len(rows) == 30
    by_team = {r.team: r for r in rows}
    assert "ARI" in by_team  # savant calls it AZ
    assert "AZ" not in by_team
    chc = by_team["CHC"]
    assert chc.pa > 1000 and 0.250 < (chc.xwoba or 0) < 0.400


def test_pitcher_expected_flips_names_and_reads_xera():
    rows = source().pitcher_expected(2026)
    sandy = next(r for r in rows if r.name == "Sandy Alcantara")
    assert sandy.xera is not None and 1.5 < sandy.xera < 7.0
    assert sandy.xwoba is not None


def test_storage_scout_join(tmp_path):
    storage = Storage(tmp_path / "odds.sqlite")
    go = make_game_odds(away="ATL", home="NYY",
                        start_time=datetime(2026, 8, 8, 19, 5, tzinfo=UTC))
    storage.store([go])
    now = datetime.now(UTC)
    storage.upsert_probables(go.game.game_id, "Chris Sale", "Gerrit Cole", fetched_at=now)
    storage.upsert_statcast_team(
        [("ATL", 2026, 4000, 0.251, 0.401, 0.315), ("NYY", 2026, 4100, 0.260, 0.440, 0.330)],
        fetched_at=now,
    )
    storage.upsert_statcast_pitcher(
        [("Gerrit Cole", 2026, 600, 0.220, 0.370, 0.290, 3.4)], fetched_at=now
    )
    scout = storage.scout(go.game.game_id)
    assert scout is not None
    assert scout["home_pitcher"] == "Gerrit Cole"
    assert scout["home_pitcher_line"]["xera"] == 3.4
    assert scout["away_pitcher_line"] is None  # Sale not in the pitcher table
    assert scout["away_batting"]["xwoba"] == 0.315
    assert storage.scout("nope") is None
    # Upsert refresh: probables firm up.
    storage.upsert_probables(go.game.game_id, "Spencer Strider", "Gerrit Cole", fetched_at=now)
    scout = storage.scout(go.game.game_id)
    assert scout is not None and scout["away_pitcher"] == "Spencer Strider"
    storage.close()


def test_schedule_games_converge_with_later_odds(tmp_path):
    """A schedule-only game must adopt the same game_id an odds poll later
    resolves to — otherwise the scout card and the odds board would describe
    two different rows for one physical game."""
    from mlb_odds.providers.espn import ESPN

    payload = json.loads((FIXTURES / "espn_mlb_scoreboard_20260808.json").read_text())
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    storage = Storage(tmp_path / "odds.sqlite")
    schedule = ESPN(sport="mlb", transport=transport).fetch_schedule(date(2026, 8, 8))
    assert storage.store_games(schedule) == 4
    atl_id = next(g.game_id for g in schedule if g.away_team == "ATL")

    odds_poll = make_game_odds(
        away="ATL", home="NYY",
        start_time=datetime(2026, 8, 8, 19, 5, tzinfo=UTC),
        provider="the_odds_api",
    )
    storage.store([odds_poll])
    assert odds_poll.game.game_id == atl_id  # converged, no duplicate row
    assert storage.store_games(schedule) == 4  # idempotent upsert, no dupes
    assert len(storage.games(date(2026, 8, 8))) == 4
    storage.close()


def test_statcast_cli_end_to_end(tmp_path, monkeypatch):
    from mlb_odds.providers.espn import ESPN

    espn_payload = json.loads((FIXTURES / "espn_mlb_scoreboard_20260808.json").read_text())
    espn_transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json=espn_payload)
    )
    monkeypatch.setattr(
        "mlb_odds.cli.ESPN",
        lambda sport="mlb": ESPN(sport=sport, transport=espn_transport),
    )
    monkeypatch.setattr(
        "mlb_odds.cli.statcast_mod.StatcastSource",
        lambda: StatcastSource(transport=statcast_transport()),
    )
    db = tmp_path / "odds.sqlite"
    result = runner.invoke(app, ["statcast", "--date", "2026-08-08", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "4/4 probables matched" in result.output
    assert "30 team" in result.output

    storage = Storage(db)
    game = next(g for g in storage.games(date(2026, 8, 8)) if g.away_team == "ATL")
    scout = storage.scout(game.game_id)
    assert scout is not None
    assert scout["home_pitcher"] == "Gerrit Cole"
    assert scout["home_batting"] is not None  # savant team line joined
    storage.close()
