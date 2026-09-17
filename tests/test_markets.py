"""One-game market view (D-045): every quote priced at its own line against
the market fair and the model, ranked by EV. Expectations are computed by
hand from the normal margin model, so a change to the conversion shows up
as a number, not a shape."""

from datetime import UTC, datetime, timedelta
from statistics import NormalDist

import pytest
from fastapi.testclient import TestClient

from conftest import make_game_odds, make_nfl_spread_odds
from mlb_odds import api, markets, valuation
from mlb_odds.models import Game, Quote
from mlb_odds.storage import Storage

N = NormalDist()
KICK = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)


def game(away="KC", home="LAC", start=KICK):
    return Game(
        game_id=f"{start.date()}-{away}-{home}-1",
        start_time=start,
        home_team=home,
        away_team=away,
        provider_ids={},
    )


def ml(book, home, away):
    return [
        Quote(book=book, market="moneyline", outcome="home", price=home),
        Quote(book=book, market="moneyline", outcome="away", price=away),
    ]


def spread(book, home_line, home_price=-110, away_price=-110, market="spread"):
    return [
        Quote(book=book, market=market, outcome="home", line=home_line, price=home_price),
        Quote(book=book, market=market, outcome="away", line=-home_line, price=away_price),
    ]


def total(book, line, over=-110, under=-110):
    return [
        Quote(book=book, market="total", outcome="over", line=line, price=over),
        Quote(book=book, market="total", outcome="under", line=line, price=under),
    ]


# --- conversions -------------------------------------------------------------------


def test_cover_and_over_probabilities_follow_the_normal_model():
    sigma = markets.MARGIN_SIGMA["nfl"]
    assert markets.cover_prob(3.0, -3.0, sigma) == 0.5  # laying exactly the expected margin
    assert markets.cover_prob(3.0, 0.0, sigma) == round(N.cdf(3.0 / sigma), 4)
    assert markets.cover_prob(3.0, -7.0, sigma) == round(N.cdf(-4.0 / sigma), 4)
    assert markets.over_prob(44.0, 44.0, 13.6) == 0.5
    assert markets.over_prob(44.0, 41.5, 13.6) == round(1 - N.cdf(-2.5 / 13.6), 4)
    assert markets.margin_from_prob(0.5, sigma) == 0.0
    assert markets.margin_from_prob(0.75, 3.9) == round(N.inv_cdf(0.75) * 3.9, 2)


# --- rows ------------------------------------------------------------------------------


def test_nfl_rows_price_each_book_at_its_own_line():
    g = game()
    quotes = (
        ml("circa", -150, 130)
        + ml("draftkings", -140, 125)
        + spread("circa", -3.0, -110, -110)
        + spread("draftkings", -3.5, -105, -115)
        + total("circa", 44.5)
        + total("draftkings", 45.5, -105, -115)
    )
    rows, cons = markets.build_rows(
        "nfl", g, quotes, fair_home=0.58, model_home=0.62, predicted_margin=5.0
    )
    assert cons.spread == -3.25 and cons.total == 45.0 and cons.moneyline_home == 0.58
    assert cons.expected_margin == 3.25 and cons.model_margin == 5.0  # NFL: spread market rules
    by = {(r.market, r.side, r.book): r for r in rows}

    # Moneyline: consensus fair on both sides, EV vs each book's price.
    dk_home = by[("moneyline", "home", "draftkings")]
    assert dk_home.fair_prob == 0.58
    assert dk_home.ev == valuation.expected_value(0.58, -140)
    assert dk_home.model_ev == valuation.expected_value(0.62, -140)
    assert by[("moneyline", "away", "circa")].fair_prob == pytest.approx(0.42)

    # Spread: DK lays 3.5 against a 3.25 consensus -> home gets 0.25 less.
    dk_sp = by[("spread", "home", "draftkings")]
    assert dk_sp.label == "LAC -3.5" and dk_sp.line == -3.5
    assert dk_sp.fair_prob == markets.cover_prob(3.25, -3.5, markets.MARGIN_SIGMA["nfl"])
    assert dk_sp.model_prob == markets.cover_prob(5.0, -3.5, markets.MARGIN_SIGMA["nfl"])
    assert dk_sp.line_edge == -0.25
    assert by[("spread", "away", "draftkings")].line_edge == 0.25
    assert by[("spread", "away", "draftkings")].label == "KC +3.5"
    # Circa's -3 vs consensus -3.25 crosses nothing; a line on the other side of 3 would.
    assert by[("spread", "home", "circa")].key_numbers == []

    # Totals: no model, fair from the consensus total through the total sigma.
    dk_over = by[("total", "over", "draftkings")]
    assert dk_over.fair_prob == markets.over_prob(45.0, 45.5, markets.TOTAL_SIGMA["nfl"])
    assert dk_over.model_prob is None and dk_over.model_ev is None
    assert dk_over.line_edge == -0.5 and by[("total", "under", "draftkings")].line_edge == 0.5

    # Ranked by EV, best per side marked exactly once.
    evs = [r.ev for r in rows]
    assert evs == sorted(evs, reverse=True)
    bests = [(r.market, r.side) for r in rows if r.best]
    assert sorted(bests) == sorted({(r.market, r.side) for r in rows})
    assert by[("moneyline", "home", "draftkings")].best  # -140 beats -150 on the same fair


def test_key_numbers_flag_a_book_across_three():
    g = game()
    quotes = spread("circa", -2.5) + spread("draftkings", -3.5) + spread("fanduel", -3.5)
    rows, cons = markets.build_rows(
        "nfl", g, quotes, fair_home=None, model_home=None, predicted_margin=None
    )
    assert cons.spread == -3.5
    circa = next(r for r in rows if r.book == "circa" and r.side == "home")
    assert circa.key_numbers == [-3.0]  # signed: the home side crossed -3
    assert circa.fair_prob == markets.cover_prob(3.5, -2.5, markets.MARGIN_SIGMA["nfl"])


def test_mlb_run_line_is_priced_from_the_moneyline_not_across_the_flip():
    """The Valtrac false positive: a book with the run line flipped to the
    other favourite must be priced for the line it hangs. CWS ~52% to win
    -> CWS -1.5 is ~37%, so +170 is roughly fair, not +50% EV."""
    g = game(away="DET", home="CWS")
    quotes = (
        ml("draftkings", -117, -103)
        + spread("draftkings", 1.5, -196, 161, market="run_line")  # DK: CWS +1.5 / DET -1.5
        + spread("tnt", -1.5, 170, -200, market="run_line")  # TNT: CWS -1.5 / DET +1.5
    )
    fair_home = valuation.devig_pair(-117, -103)
    rows, cons = markets.build_rows(
        "mlb", g, quotes, fair_home=fair_home, model_home=None, predicted_margin=None
    )
    assert cons.expected_margin == markets.margin_from_prob(fair_home, 3.9)
    tnt = next(r for r in rows if r.book == "tnt" and r.side == "home")
    assert tnt.label == "CWS -1.5"
    assert 0.33 < tnt.fair_prob < 0.41
    assert -0.12 < tnt.ev < 0.08  # roughly fair, nowhere near +0.5
    dk = next(r for r in rows if r.book == "draftkings" and r.side == "home")
    assert dk.label == "CWS +1.5" and dk.fair_prob > 0.6


def test_props_are_devigged_within_book_and_judged_across_books():
    g = game(away="NYM", home="NYY", start=datetime(2026, 7, 9, 23, 5, tzinfo=UTC))
    quotes = [
        Quote(
            book="draftkings",
            market="batter_hits",
            outcome="over",
            line=1.5,
            price=140,
            player="Judge",
        ),
        Quote(
            book="draftkings",
            market="batter_hits",
            outcome="under",
            line=1.5,
            price=-170,
            player="Judge",
        ),
        Quote(
            book="fanduel",
            market="batter_hits",
            outcome="over",
            line=1.5,
            price=125,
            player="Judge",
        ),
        Quote(
            book="fanduel",
            market="batter_hits",
            outcome="under",
            line=1.5,
            price=-150,
            player="Judge",
        ),
        Quote(
            book="fanduel",
            market="batter_hits",
            outcome="over",
            line=0.5,
            price=-250,
            player="Judge",
        ),
    ]
    rows, _ = markets.build_rows(
        "mlb", g, quotes, fair_home=None, model_home=None, predicted_margin=None
    )
    over = {r.book: r for r in rows if r.player == "Judge" and r.side == "over" and r.line == 1.5}
    fair = (valuation.devig_pair(140, -170) + valuation.devig_pair(125, -150)) / 2
    assert over["draftkings"].fair_prob == round(fair, 4)
    assert over["draftkings"].ev == valuation.expected_value(fair, 140)
    assert over["draftkings"].best and not over["fanduel"].best  # +140 beats +125 on one fair
    lone = next(r for r in rows if r.player == "Judge" and r.line == 0.5)
    assert lone.fair_prob is None and lone.ev is None  # no under quoted: unpriced


# --- endpoint --------------------------------------------------------------------------


@pytest.fixture
def nfl_db(tmp_path, monkeypatch):
    db = tmp_path / "nfl.sqlite"
    storage = Storage(db)
    try:
        go = make_nfl_spread_odds(
            {"circa": -3.0, "draftkings": -3.5},
            KICK - timedelta(days=3),
            start_time=KICK,
            moneylines={"circa": (130, -150), "draftkings": (125, -140)},
        )
        storage.store([go])
        prior = make_nfl_spread_odds(
            {"circa": -1.0},
            KICK - timedelta(days=10),
            away="LAC",
            home="LV",
            start_time=KICK - timedelta(days=7),
        )
        storage.store([prior])
        game_id = go.game.game_id
    finally:
        storage.close()
    monkeypatch.setenv("NFL_ODDS_DB", str(db))
    return game_id


def test_markets_endpoint_nfl(nfl_db):
    client = TestClient(api.app, raise_server_exceptions=False)
    body = client.get(f"/api/games/{nfl_db}/markets", params={"sport": "nfl"}).json()
    assert (body["away_team"], body["home_team"]) == ("KC", "LAC")
    ctx = body["context"]
    assert ctx["consensus_spread"] == -3.25 and ctx["expected_margin"] == 3.25
    assert ctx["consensus_prob"] is not None and ctx["books"] == 2
    assert ctx["home_rest"] == 7 and ctx["away_rest"] is None and ctx["divisional"] is True
    assert ctx["snapshots"] == 1 and ctx["first_seen"] == ctx["last_seen"]
    rows = body["rows"]
    assert {r["market"] for r in rows} == {"moneyline", "spread"}
    assert len(rows) == 8  # 2 books x (ML home/away + spread home/away)
    assert [r["ev"] for r in rows] == sorted((r["ev"] for r in rows), reverse=True)
    assert sum(r["best"] for r in rows) == 4


def test_markets_endpoint_mlb_with_props_and_404(tmp_path, monkeypatch):
    db = tmp_path / "odds.sqlite"
    storage = Storage(db)
    try:
        go = make_game_odds()
        storage.store([go])
        prop = make_game_odds(
            quotes=[
                Quote(
                    book="draftkings",
                    market="batter_hits",
                    outcome="over",
                    line=1.5,
                    price=140,
                    player="Judge",
                ),
                Quote(
                    book="draftkings",
                    market="batter_hits",
                    outcome="under",
                    line=1.5,
                    price=-170,
                    player="Judge",
                ),
            ],
            fetched_at=go.fetched_at + timedelta(hours=1),
        )
        storage.store([prop])
    finally:
        storage.close()
    monkeypatch.setenv("MLB_ODDS_DB", str(db))
    client = TestClient(api.app, raise_server_exceptions=False)
    body = client.get(f"/api/games/{go.game.game_id}/markets").json()
    markets_seen = {r["market"] for r in body["rows"]}
    assert markets_seen == {"moneyline", "run_line", "total", "batter_hits"}
    judge = [r for r in body["rows"] if r["player"] == "Judge"]
    assert len(judge) == 2 and all(r["fair_prob"] is not None for r in judge)
    assert body["context"]["consensus_total"] == 8.5
    assert client.get("/api/games/2026-07-09-XXX-YYY-1/markets").status_code == 404
    assert client.get("/api/games/nonsense/markets").status_code == 404


def test_stale_books_are_shown_but_never_ranked_or_starred():
    """A book that stopped reporting keeps its last quote (carry-forward);
    six weeks on it looks like value. It must sort last and never be best."""
    g = game()
    quotes = ml("betus", 200, -250) + ml("draftkings", -140, 125)  # betus: an ancient +200
    fresh = KICK - timedelta(days=1)
    quoted = {
        ("betus", "moneyline"): fresh - timedelta(days=40),
        ("draftkings", "moneyline"): fresh,
    }
    rows, _ = markets.build_rows(
        "nfl", g, quotes, fair_home=0.58, model_home=None, predicted_margin=None, quoted_at=quoted
    )
    betus_home = next(r for r in rows if r.book == "betus" and r.side == "home")
    assert betus_home.stale and betus_home.ev > 0.5  # the bait
    assert not betus_home.best
    assert rows[-1].book == "betus" and rows[-2].book == "betus"  # stale rows sink
    dk_home = next(r for r in rows if r.book == "draftkings" and r.side == "home")
    assert dk_home.best and not dk_home.stale
    assert dk_home.quoted_at == fresh
    # Within a day of the newest snapshot is fresh.
    near = {("betus", "moneyline"): fresh - timedelta(hours=20), ("draftkings", "moneyline"): fresh}
    rows, _ = markets.build_rows(
        "nfl", g, quotes, fair_home=0.58, model_home=None, predicted_margin=None, quoted_at=near
    )
    assert not any(r.stale for r in rows)


def test_endpoint_marks_a_book_that_stopped_reporting(tmp_path, monkeypatch):
    db = tmp_path / "nfl.sqlite"
    storage = Storage(db)
    try:
        old = make_nfl_spread_odds(
            {"betus": -1.0},
            KICK - timedelta(days=40),
            start_time=KICK,
            moneylines={"betus": (200, -250)},
        )
        storage.store([old])
        new = make_nfl_spread_odds(
            {"draftkings": -3.5},
            KICK - timedelta(days=1),
            start_time=KICK,
            moneylines={"draftkings": (125, -140)},
        )
        storage.store([new])
        game_id = new.game.game_id
    finally:
        storage.close()
    monkeypatch.setenv("NFL_ODDS_DB", str(db))
    client = TestClient(api.app, raise_server_exceptions=False)
    body = client.get(f"/api/games/{game_id}/markets", params={"sport": "nfl"}).json()
    stale = {r["book"] for r in body["rows"] if r["stale"]}
    assert stale == {"betus"}
    assert all(not r["best"] for r in body["rows"] if r["stale"])
    assert body["rows"][-1]["book"] == "betus"
    assert all(r["quoted_at"] for r in body["rows"])
