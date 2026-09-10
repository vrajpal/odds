"""Circa contest-sheet reader (D-042): URL discovery, the pure parser on
recorded OCR tokens, the store's sheet audit, the CLI, and — when the OCR
extra is installed — the real render+OCR path on the real Week 1 PDF.

Ground truth is the Week 1 sheet itself (tests/fixtures/circa_million_viii_
week1.pdf), transcribed by eye: every expectation below was checked against
the image, not against the parser."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from typer.testing import CliRunner

from conftest import FIXTURES, make_nfl_spread_odds
from mlb_odds import circa, contest
from mlb_odds.cli import app
from mlb_odds.models import Game
from mlb_odds.providers.base import ProviderError
from mlb_odds.storage import Storage

runner = CliRunner()
PDF = FIXTURES / "circa_million_viii_week1.pdf"
TOKENS = FIXTURES / "circa_sheet_week1_tokens.json"
FETCH_AT = datetime(2026, 9, 9, 17, 0, tzinfo=UTC)

# The 2026 Week 1 schedule (away, home, kickoff UTC) and the sheet's home
# spreads, read off the sheet by eye.
WEEK1 = [
    ("NE", "SEA", "2026-09-10T00:15:00+00:00", -3.0),
    ("SF", "LAR", "2026-09-11T00:35:00+00:00", -4.0),
    ("ATL", "PIT", "2026-09-13T17:00:00+00:00", -3.5),
    ("BAL", "IND", "2026-09-13T17:00:00+00:00", 3.5),
    ("BUF", "HOU", "2026-09-13T17:00:00+00:00", 0.5),
    ("CHI", "CAR", "2026-09-13T17:00:00+00:00", 3.0),
    ("TB", "CIN", "2026-09-13T17:00:00+00:00", -3.5),
    ("CLE", "JAX", "2026-09-13T17:00:00+00:00", -8.5),
    ("NO", "DET", "2026-09-13T17:00:00+00:00", -7.0),
    ("NYJ", "TEN", "2026-09-13T17:00:00+00:00", -1.5),
    ("ARI", "LAC", "2026-09-13T20:25:00+00:00", -8.5),
    ("GB", "MIN", "2026-09-13T20:25:00+00:00", -1.5),
    ("MIA", "LV", "2026-09-13T20:25:00+00:00", -3.5),
    ("WAS", "PHI", "2026-09-13T20:25:00+00:00", -4.5),
    ("DAL", "NYG", "2026-09-14T00:20:00+00:00", 3.0),
    ("DEN", "KC", "2026-09-15T00:15:00+00:00", -3.0),
]


def week1_games() -> list[Game]:
    return [
        Game(
            game_id=f"{start[:10]}-{away}-{home}-1",
            start_time=datetime.fromisoformat(start),
            home_team=home,
            away_team=away,
            provider_ids={},
        )
        for away, home, start, _spread in WEEK1
    ]


EXPECTED = {f"{start[:10]}-{away}-{home}-1": spread for away, home, start, spread in WEEK1}


def fixture_tokens() -> list[circa.Token]:
    raw = json.loads(TOKENS.read_text())
    return [circa.Token(t["x"], t["y"], t["h"], t["text"], t["conf"]) for t in raw["tokens"]]


# --- spreads and team names -----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("+3", 3.0),
        ("-7", -7.0),
        ("+8½", 8.5),
        ("-8½", -8.5),
        ("+½", 0.5),
        ("-½", -0.5),
        ("+8%", 8.5),  # what OCR makes of the ½ glyph
        ("-%", -0.5),
        ("+y", 0.5),
        ("+ 1 1/2", 1.5),
        ("PK", 0.0),
        ("EVEN", 0.0),
        ("Sep 13", None),
        ("10:00AM", None),
        ("+", None),
        ("-", None),
        ("3", None),  # unsigned: a rotation number, not a spread
        ("-100", None),
        ("COLTS", None),
    ],
)
def test_parse_spread(text, value):
    assert circa.parse_spread(text) == value


def test_resolve_team_by_nickname_suffix_with_ocr_fixes():
    cands = circa.nicknames_for(week1_games())
    assert circa.resolve_team("Sep 13 18 JETS", cands) == "NYJ"
    assert circa.resolve_team("22CARDINALS", cands) == "ARI"
    assert circa.resolve_team("449ERS", cands) == "SF"  # rotation 4 + 49ERS
    assert circa.resolve_team("D0LPHINS", cands) == "MIA"
    assert circa.resolve_team("C0WB0YS", cands) == "DAL"
    assert circa.resolve_team("BUCS", cands) == "TB"  # alias
    assert circa.resolve_team("26C0MMANDER", cands) == "WAS"  # truncated: close match
    assert circa.resolve_team("Sep 136", cands) is None
    assert circa.resolve_team("MILLION", cands) is None  # LIONS is not a suffix
    assert circa.resolve_team("", cands) is None


def test_candidates_are_only_the_teams_playing_this_week():
    games = [g for g in week1_games() if "NYJ" not in (g.home_team, g.away_team)]
    cands = circa.nicknames_for(games)
    assert circa.resolve_team("JETS", cands) is None
    assert circa.resolve_team("TITANS", cands) is None  # its opponent left too
    assert circa.resolve_team("CHIEFS", cands) == "KC"


# --- URL discovery ---------------------------------------------------------------


def test_sheet_urls_try_the_expected_month_then_its_neighbours():
    urls = circa.sheet_urls(1, contest.lines_post_time(1))
    assert urls[0] == (
        "https://www.circasports.com/wp-content/uploads/2026/09/"
        "Circa-Sports-Million-VIII-Contest-Point-Spreads-Week-1.pdf"
    )
    assert "/2026/08/" in urls[1] and "/2026/10/" in urls[2]
    december = circa.sheet_urls(17, datetime(2026, 12, 31, 18, tzinfo=UTC))
    assert ["/2026/12/" in december[0], "/2026/11/" in december[1], "/2027/01/" in december[2]]
    assert all(u.endswith("Week-17.pdf") for u in december)


def _transport(responses: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return responses.get(str(request.url), httpx.Response(404))

    return httpx.MockTransport(handler)


def test_fetch_falls_through_404s_to_the_posted_sheet():
    post = contest.lines_post_time(1)
    urls = circa.sheet_urls(1, post)
    source = circa.CircaSheets(
        transport=_transport({urls[1]: httpx.Response(200, content=b"%PDF-1.4 x")})
    )
    sheet = source.fetch(1, post)
    assert sheet is not None and sheet.source == urls[1] and sheet.is_pdf
    assert circa.CircaSheets(transport=_transport({})).fetch(1, post) is None


def test_fetch_rejects_non_pdf_and_server_errors():
    post = contest.lines_post_time(1)
    url = circa.sheet_urls(1, post)[0]
    html = circa.CircaSheets(
        transport=_transport(
            {url: httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})}
        )
    )
    with pytest.raises(ProviderError, match="not a PDF"):
        html.fetch(1, post)
    broken = circa.CircaSheets(transport=_transport({url: httpx.Response(503)}))
    with pytest.raises(ProviderError, match="503"):
        broken.fetch(1, post)


# --- the parser on recorded tokens --------------------------------------------------


def test_week1_tokens_parse_to_every_game_on_the_sheet():
    tokens = fixture_tokens()
    parsed = circa.parse_tokens(tokens, week1_games())
    assert parsed.problems == []
    assert {line.game_id: line.home_spread for line in parsed.lines} == EXPECTED
    assert len(parsed.readings) == 32  # both sides of all 16 games


def test_mismatched_sides_are_reported_not_stored():
    tokens = fixture_tokens()
    # SEAHAWKS -3 -> -4 while PATRIOTS stays +3.
    bad = [
        circa.Token(t.x, t.y, t.h, "-4", t.conf) if t.text == "-3" and t.y < 400 else t
        for t in tokens
    ]
    parsed = circa.parse_tokens(bad, week1_games())
    assert len(parsed.lines) == 15
    assert any("NE @ SEA" in p and "don't mirror" in p for p in parsed.problems)
    assert "2026-09-10-NE-SEA-1" not in {line.game_id for line in parsed.lines}


def test_one_side_missing_is_reported():
    tokens = fixture_tokens()
    without = [t for t in tokens if not (t.text == "+3" and t.y < 400)]  # drop PATRIOTS +3
    parsed = circa.parse_tokens(without, week1_games())
    assert any(p == "NE @ SEA: only the home side read (-3)" for p in parsed.problems)
    assert len(parsed.lines) == 15


def test_game_absent_from_sheet_and_text_absent_from_schedule():
    tokens = fixture_tokens()
    no_kc = [t for t in tokens if "BR0NCOS" not in t.text and "CHIEFS" not in t.text]
    parsed = circa.parse_tokens(no_kc, week1_games())
    # The spreads are still there with no team text to their left that resolves.
    assert any("DEN @ KC" in p for p in parsed.problems)
    assert "2026-09-15-DEN-KC-1" not in {line.game_id for line in parsed.lines}

    schedule_without_kc = [g for g in week1_games() if g.home_team != "KC"]
    parsed = circa.parse_tokens(tokens, schedule_without_kc)
    assert any("could not match" in p and "CHIEFS" in p for p in parsed.problems)
    assert len(parsed.lines) == 15


def test_duplicate_reading_is_reported():
    tokens = fixture_tokens()
    dup = tokens + [
        circa.Token(t.x, t.y + 2000, t.h, t.text, t.conf)
        for t in tokens
        if t.text in ("+3", "Sep 9 2 PATRIOTS") and t.y < 400
    ]
    parsed = circa.parse_tokens(dup, week1_games())
    assert any(p.startswith("NE read 2 times") for p in parsed.problems)
    assert "2026-09-10-NE-SEA-1" not in {line.game_id for line in parsed.lines}


# --- the store's sheet audit -------------------------------------------------------


def test_contest_store_records_sheets_by_hash(tmp_path):
    store = contest.ContestStore(tmp_path / "contest.sqlite")
    try:
        assert store.sheets(1) == []
        store.record_sheet(
            1, sha256="abc", source="u", fetched_at=FETCH_AT, lines_stored=15, problems=["x", "y"]
        )
        store.record_sheet(
            1, sha256="abc", source="u2", fetched_at=FETCH_AT, lines_stored=16, problems=[]
        )
        (rec,) = store.sheets(1)
        assert (rec.source, rec.lines_stored, rec.problems) == ("u2", 16, [])
        assert rec.fetched_at == FETCH_AT
    finally:
        store.close()


# --- CLI ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    nfl_db = tmp_path / "nfl.sqlite"
    storage = Storage(nfl_db)
    try:
        for game in week1_games():
            storage.store(
                [
                    make_nfl_spread_odds(
                        {"circa": -3.0},
                        FETCH_AT,
                        away=game.away_team,
                        home=game.home_team,
                        start_time=game.start_time,
                    )
                ]
            )
    finally:
        storage.close()
    tokens = fixture_tokens()
    monkeypatch.setattr(circa, "render", lambda sheet: object())
    monkeypatch.setattr(circa, "ocr", lambda image: tokens)
    return {"nfl_db": nfl_db, "contest_db": tmp_path / "contest.sqlite", "tokens": tokens}


def run(env, *extra: str):
    return runner.invoke(
        app,
        [
            "contest-lines",
            "--week",
            "1",
            "--db",
            str(env["nfl_db"]),
            "--contest-db",
            str(env["contest_db"]),
            *extra,
        ],
    )


def test_cli_stores_lines_saves_sheet_and_is_idempotent(env):
    result = run(env, "--file", str(PDF))
    assert result.exit_code == 0, result.output
    assert "16 contest line(s) stored for week 1 (0 problem(s))" in result.output
    assert "DEN @ KC     -3   market -3" in result.output
    saved = env["contest_db"].parent / "contest-sheets" / "week-1.pdf"
    assert saved.read_bytes() == PDF.read_bytes()

    store = contest.ContestStore(env["contest_db"])
    try:
        lines = store.lines(1)
        assert {g: line.home_spread for g, line in lines.items()} == EXPECTED
        first_entry = lines["2026-09-10-NE-SEA-1"].entered_at
        (rec,) = store.sheets(1)
        assert rec.lines_stored == 16 and rec.problems == [] and rec.source == str(PDF)
    finally:
        store.close()

    again = run(env, "--file", str(PDF))
    assert again.exit_code == 0 and "already processed" in again.output
    store = contest.ContestStore(env["contest_db"])
    try:
        assert store.lines(1)["2026-09-10-NE-SEA-1"].entered_at == first_entry
    finally:
        store.close()

    forced = run(env, "--file", str(PDF), "--force")
    assert forced.exit_code == 0 and "16 contest line(s) stored" in forced.output


def test_cli_dry_run_stores_nothing(env):
    result = run(env, "--file", str(PDF), "--dry-run")
    assert result.exit_code == 0, result.output
    assert "dry run: 16 line(s) read, 0 problem(s)." in result.output
    assert not (env["contest_db"].parent / "contest-sheets").exists()
    store = contest.ContestStore(env["contest_db"])
    try:
        assert store.lines(1) == {} and store.sheets(1) == []
    finally:
        store.close()


def test_cli_problems_store_the_good_lines_and_exit_1(env, monkeypatch):
    tokens = [
        circa.Token(t.x, t.y, t.h, "-4", t.conf) if t.text == "-3" and t.y < 400 else t
        for t in env["tokens"]
    ]
    monkeypatch.setattr(circa, "ocr", lambda image: tokens)
    result = run(env, "--file", str(PDF))
    assert result.exit_code == 1
    assert "✗ NE @ SEA: sides don't mirror" in result.output
    assert "15 contest line(s) stored for week 1 (1 problem(s))" in result.output
    store = contest.ContestStore(env["contest_db"])
    try:
        assert len(store.lines(1)) == 15
        (rec,) = store.sheets(1)
        assert rec.lines_stored == 15 and len(rec.problems) == 1
    finally:
        store.close()


def test_cli_flags_a_line_far_from_market_and_a_changed_entry(env):
    store = contest.ContestStore(env["contest_db"])
    try:
        store.set_line(1, "2026-09-15-DEN-KC-1", -6.0, entered_at=FETCH_AT)  # a hand entry
    finally:
        store.close()
    result = run(env, "--file", str(PDF))
    assert "(was -6)" in result.output
    # Market fixture is -3 everywhere: JAX -8.5 vs -3 is within 7, LAC -8.5 too;
    # nothing on this sheet is > 7 off, so the warning must be absent.
    assert "far from market" not in result.output


def test_cli_polls_and_exits_quietly_before_the_post(env, monkeypatch):
    real = circa.CircaSheets
    monkeypatch.setattr(circa, "CircaSheets", lambda: real(transport=_transport({})))
    result = run(env)
    assert result.exit_code == 0 and "not posted yet" in result.output
    store = contest.ContestStore(env["contest_db"])
    try:
        assert store.lines(1) == {}
    finally:
        store.close()


def test_cli_fetches_the_posted_sheet(env, monkeypatch):
    post = contest.lines_post_time(1)
    url = circa.sheet_urls(1, post)[0]
    real = circa.CircaSheets
    monkeypatch.setattr(
        circa,
        "CircaSheets",
        lambda: real(transport=_transport({url: httpx.Response(200, content=PDF.read_bytes())})),
    )
    result = run(env)
    assert result.exit_code == 0, result.output
    assert f"Week 1 sheet: {url}" in result.output
    store = contest.ContestStore(env["contest_db"])
    try:
        (rec,) = store.sheets(1)
        assert rec.source == url and rec.lines_stored == 16
    finally:
        store.close()


def test_cli_needs_stored_games(tmp_path, env):
    empty = tmp_path / "empty.sqlite"
    Storage(empty).close()
    result = runner.invoke(
        app,
        [
            "contest-lines",
            "--week",
            "1",
            "--file",
            str(PDF),
            "--db",
            str(empty),
            "--contest-db",
            str(tmp_path / "c.sqlite"),
        ],
    )
    assert result.exit_code == 1 and "no stored NFL games" in result.output


# --- the real thing (needs the `sheet` extra) --------------------------------------


def test_real_render_and_ocr_read_the_week1_pdf():
    pytest.importorskip("pypdfium2")
    pytest.importorskip("rapidocr_onnxruntime")
    sheet = circa.Sheet(source=str(PDF), data=PDF.read_bytes())
    assert sheet.is_pdf
    parsed = circa.read_sheet(sheet, week1_games())
    assert parsed.problems == []
    assert {line.game_id: line.home_spread for line in parsed.lines} == EXPECTED
