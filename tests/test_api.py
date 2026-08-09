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


def test_root_serves_built_frontend_when_dist_exists(seeded_db, tmp_path, monkeypatch):
    """Routes match in registration order; a JSON "/" route registered before
    the static mount once shadowed it, so the built UI was unreachable."""
    import importlib

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>mlb-odds ui</body></html>")
    monkeypatch.setenv("MLB_ODDS_FRONTEND_DIST", str(dist))
    try:
        importlib.reload(api)
        resp = TestClient(api.app, raise_server_exceptions=False).get("/")
        assert resp.status_code == 200
        assert "mlb-odds ui" in resp.text
        assert TestClient(api.app).get("/api/health").json()["status"] == "ok"
    finally:
        monkeypatch.delenv("MLB_ODDS_FRONTEND_DIST")
        importlib.reload(api)


def test_root_returns_json_when_no_dist(seeded_db, tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("MLB_ODDS_FRONTEND_DIST", str(tmp_path / "absent"))
    try:
        importlib.reload(api)
        resp = TestClient(api.app, raise_server_exceptions=False).get("/")
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.json()["status"] == "ok"
    finally:
        monkeypatch.delenv("MLB_ODDS_FRONTEND_DIST")
        importlib.reload(api)


def test_export_rejects_bad_format(client):
    assert client.get("/api/export?fmt=xml").status_code == 400


class TestSportSwitcher:
    """?sport= reads that sport's own database (D-019) and renders its
    spread-market name."""

    @pytest.fixture
    def nfl_db(self, tmp_path, monkeypatch):
        from conftest import make_nfl_spread_odds

        db = tmp_path / "nfl-odds.sqlite"
        storage = Storage(db)
        # Noon local-time today: always inside the board's local-day window,
        # unlike now+3h which crosses midnight when the suite runs late.
        local_noon = datetime.now().astimezone().replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        storage.store(
            [
                make_nfl_spread_odds(
                    {"circa": -3.5},
                    datetime.now(UTC),
                    start_time=local_noon.astimezone(UTC),
                )
            ]
        )
        storage.close()
        monkeypatch.setenv("NFL_ODDS_DB", str(db))
        return db

    def test_nfl_board_uses_spread_key(self, client, nfl_db):
        body = client.get("/api/today", params={"sport": "nfl"}).json()
        (game,) = body
        assert game["game"]["away_team"] == "KC"
        (odds,) = game["books"].values()
        assert set(odds) == {"moneyline", "spread", "total"}
        assert odds["spread"] == "-3.5 (-110)"

    def test_mlb_board_still_uses_run_line_key(self, client):
        body = client.get("/api/today").json()
        if body:  # seeded MLB game may not be "today" — key shape is the point
            assert "run_line" in next(iter(body[0]["books"].values()))

    def test_sports_read_separate_databases(self, client, nfl_db):
        nfl = client.get("/api/today", params={"sport": "nfl"}).json()
        mlb_ids = {g["game"]["game_id"] for g in client.get("/api/today").json()}
        assert {g["game"]["game_id"] for g in nfl}.isdisjoint(mlb_ids)

    def test_invalid_sport_is_422(self, client):
        assert client.get("/api/today", params={"sport": "nhl"}).status_code == 422

    def test_missing_nfl_db_503_names_the_flag(self, tmp_path, monkeypatch, seeded_db):
        monkeypatch.setenv("NFL_ODDS_DB", str(tmp_path / "absent" / "nfl.sqlite"))
        c = TestClient(api.app, raise_server_exceptions=False)
        resp = c.get("/api/today", params={"sport": "nfl"})
        assert resp.status_code == 503
        assert "--sport nfl" in resp.json()["detail"]


class TestManualRefresh:
    """POST /api/refresh (D-029): the one sanctioned provider-reaching
    endpoint — free ESPN only, debounced, no key ever needed."""

    @pytest.fixture(autouse=True)
    def _reset_debounce(self, monkeypatch, tmp_path):
        monkeypatch.setattr(api, "_last_refresh", {})
        monkeypatch.setenv("MLB_ODDS_DB", str(tmp_path / "odds.sqlite"))
        monkeypatch.setenv("NFL_ODDS_DB", str(tmp_path / "nfl.sqlite"))
        monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)

    @pytest.fixture
    def espn_stub(self, monkeypatch):
        from conftest import fixture_transport
        from mlb_odds.providers.espn import ESPN

        monkeypatch.setattr(
            api, "ESPN",
            lambda sport="mlb": ESPN(
                sport=sport,
                transport=fixture_transport(
                    "espn_scoreboard_normal" if sport == "mlb"
                    else "espn_nfl_scoreboard_normal"
                ),
            ),
        )
        return TestClient(api.app, raise_server_exceptions=False)

    def test_refresh_pulls_and_stores_without_metered_key(self, espn_stub):
        # THE_ODDS_API_KEY is unset: proves the endpoint cannot touch the
        # metered provider even by accident.
        body = espn_stub.post("/api/refresh").json()
        assert body["games"] > 0 and body["rows"] > 0
        assert body["errors"] == {}

    def test_refresh_is_debounced_per_sport(self, espn_stub):
        assert espn_stub.post("/api/refresh").status_code == 200
        again = espn_stub.post("/api/refresh")
        assert again.status_code == 429
        assert "retry in" in again.json()["detail"]
        # A different sport has its own debounce clock.
        assert espn_stub.post("/api/refresh", params={"sport": "nfl"}).status_code == 200

    def test_refresh_rejects_unknown_sport(self, espn_stub):
        assert espn_stub.post("/api/refresh", params={"sport": "nhl"}).status_code == 422


class TestDashboard:
    """GET /api/dashboard (D-030): day board with devigged consensus, the
    strength model, and best-EV prices."""

    @pytest.fixture
    def seeded(self, tmp_path, monkeypatch):
        from mlb_odds.models import Quote as Q

        def ml(book, h, a):
            return [
                Q(book=book, market="moneyline", outcome="home", price=h),
                Q(book=book, market="moneyline", outcome="away", price=a),
            ]

        db = tmp_path / "odds.sqlite"
        storage = Storage(db)
        # Ten past games across four teams: enough for the strength fit.
        teams = ["NYY", "BOS", "TB", "BAL"]
        t = datetime(2026, 7, 20, 23, 0, tzinfo=UTC)
        for i in range(10):
            home, away = teams[i % 4], teams[(i + 1) % 4]
            storage.store([make_game_odds(
                away=away, home=home, start_time=t,
                fetched_at=t - timedelta(hours=8),
                quotes=ml("draftkings", -140, 120),
            )])
            t += timedelta(hours=27)
        # Today's game: two books, one clearly better priced per side, plus
        # run line and total.
        local_noon = datetime.now().astimezone().replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        today_quotes = (
            ml("draftkings", -150, 130)
            + ml("fanduel", -140, 125)
            + [
                Q(book="draftkings", market="run_line", outcome="home", line=-1.5, price=105),
                Q(book="draftkings", market="run_line", outcome="away", line=1.5, price=-125),
                Q(book="draftkings", market="total", outcome="over", line=8.5, price=-110),
                Q(book="draftkings", market="total", outcome="under", line=8.5, price=-110),
            ]
        )
        go = make_game_odds(
            away="BOS", home="NYY", start_time=local_noon.astimezone(UTC),
            fetched_at=datetime.now(UTC) - timedelta(hours=2), quotes=today_quotes,
        )
        storage.store([go])
        storage.close()
        monkeypatch.setenv("MLB_ODDS_DB", str(db))
        return {"game_id": go.game.game_id, "date": local_noon.date().isoformat()}

    @pytest.fixture
    def dash_client(self, seeded):
        return TestClient(api.app, raise_server_exceptions=False)

    def test_dashboard_today_valuation(self, dash_client, seeded):
        body = dash_client.get("/api/dashboard").json()
        assert body["date"] == seeded["date"]
        assert body["hfa"] is not None
        assert len(body["strengths"]) == 4
        (game,) = [g for g in body["games"] if g["game_id"] == seeded["game_id"]]

        ml = game["moneyline"]
        assert 0.5 < ml["consensus_prob"] < 0.62  # home favorite, devigged
        assert ml["model_prob"] is not None
        assert ml["model_edge"] == pytest.approx(
            ml["model_prob"] - ml["consensus_prob"], abs=1e-3
        )
        # fanduel offers the better price on both sides here.
        assert ml["best_home"]["book"] == "fanduel"
        assert ml["best_away"]["book"] == "draftkings"  # +130 beats +125
        assert set(ml["books"]) == {"draftkings", "fanduel"}

        assert game["run_line"]["draftkings"]["line"] == -1.5
        assert game["run_line"]["draftkings"]["away"] == -125
        assert game["total"]["draftkings"]["line"] == 8.5
        assert game["total"]["draftkings"]["over"] == -110

    def test_dashboard_specific_past_day(self, dash_client):
        body = dash_client.get("/api/dashboard", params={"on": "2026-07-20"}).json()
        assert body["date"] == "2026-07-20"
        assert len(body["games"]) >= 1
        # Past games have moneyline data but no run line stored.
        assert body["games"][0]["moneyline"]["consensus_prob"] is not None

    def test_dashboard_empty_day_and_bad_date(self, dash_client):
        assert dash_client.get(
            "/api/dashboard", params={"on": "2026-01-01"}
        ).json()["games"] == []
        assert dash_client.get(
            "/api/dashboard", params={"on": "not-a-date"}
        ).status_code == 422
