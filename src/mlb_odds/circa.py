"""Circa Million contest point spreads, read from Circa's own sheet (D-042).

Every week Circa posts the contest spreads as a one-page PDF on its
WordPress uploads path and tweets the link from @CircaSports. The URL is
predictable — `.../wp-content/uploads/<year>/<month>/Circa-Sports-Million-
<numeral>-Contest-Point-Spreads-Week-<n>.pdf`, verified across the whole
VII season — so this module polls the URL directly rather than scraping X
(whose public timeline endpoints rate-limit unauthenticated readers).

The PDF has no text layer: every glyph is a vector path. So the sheet is
rendered to a bitmap and read with an OCR engine (RapidOCR, ONNX, bundled
models, no network), then parsed *against the stored NFL schedule*:

- team names resolve by nickname suffix (the sheet says "COMMANDERS",
  "49ERS", "BUCS") among only the teams playing in that contest week;
- each spread pairs with the team text immediately left of it on the same
  row, so column layout never matters (the tweet's image is three columns,
  the PDF two);
- a game is accepted only when BOTH sides were read and are exact
  negatives of each other. One misread glyph breaks the symmetry and the
  game is reported as a problem instead of stored — a missing line costs a
  minute of manual entry, a wrong one costs a pick.

Rendering and OCR are optional dependencies (`uv sync --extra sheet`); the
parser is pure and runs on recorded tokens, which is what the tests use.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
from typing import Any

import httpx

from mlb_odds import teams
from mlb_odds.models import Game
from mlb_odds.providers.base import ProviderError

logger = logging.getLogger("mlb_odds.circa")

SEASON_NUMERAL = "VIII"  # Circa Sports Million VIII = the 2026 season
UPLOADS_BASE = "https://www.circasports.com/wp-content/uploads"
_USER_AGENT = "mlb-odds/0.1 (+https://github.com/vrajpal/odds)"
RENDER_SCALE = 2.5  # 612x1008 pt page -> 1530x2520 px; OCR reads this cleanly

# Sheet nicknames that aren't the last word of the team's full name.
NICKNAME_ALIASES: dict[str, str] = {
    "BUCS": "TB",
    "NINERS": "SF",
    "PATS": "NE",
    "JAGS": "JAX",
}


class SheetToolingError(RuntimeError):
    """Rendering/OCR packages are missing: `uv sync --extra sheet`."""


# --- locating the sheet ---------------------------------------------------------


def sheet_urls(week: int, post_time: datetime, numeral: str = SEASON_NUMERAL) -> list[str]:
    """Candidate PDF URLs, most likely first.

    WordPress files an upload under the month it was posted; a sheet posted
    early or late across a month boundary lands in the neighbouring folder,
    so the expected month is tried first and both neighbours after it."""
    year, month = post_time.year, post_time.month
    candidates = []
    for delta in (0, -1, 1):
        m = month + delta
        y = year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        candidates.append(
            f"{UPLOADS_BASE}/{y}/{m:02d}/Circa-Sports-Million-{numeral}"
            f"-Contest-Point-Spreads-Week-{week}.pdf"
        )
    return candidates


@dataclass(frozen=True)
class Sheet:
    source: str  # URL, or a local path for --file
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def is_pdf(self) -> bool:
        return self.data.startswith(b"%PDF")


class CircaSheets:
    """Fetches the week's contest sheet from circasports.com."""

    name = "circa"

    def __init__(self, *, transport: httpx.BaseTransport | None = None, timeout: float = 20.0):
        self._client = httpx.Client(
            headers={"User-Agent": _USER_AGENT},
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def fetch(self, week: int, post_time: datetime) -> Sheet | None:
        """The sheet if Circa has posted it, else None (poll again later)."""
        for url in sheet_urls(week, post_time):
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                raise ProviderError(f"circasports.com unreachable: {exc}") from exc
            if response.status_code == 404:
                logger.debug("no sheet at %s", url)
                continue
            if response.status_code != 200:
                raise ProviderError(f"circasports.com returned {response.status_code} for {url}")
            if not response.content.startswith(b"%PDF"):
                raise ProviderError(f"{url} is not a PDF ({response.headers.get('content-type')})")
            logger.info("week %d sheet found: %s (%d bytes)", week, url, len(response.content))
            return Sheet(source=url, data=response.content)
        return None


# --- rendering + OCR (optional dependencies) --------------------------------------


def render(sheet: Sheet) -> Any:
    """The sheet as a PIL image: the PDF's first page rasterized, or the
    image itself when a screenshot/tweet image was supplied via --file."""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise SheetToolingError("pillow is missing: uv sync --extra sheet") from exc
    if not sheet.is_pdf:
        return Image.open(io.BytesIO(sheet.data)).convert("RGB")
    try:
        import pypdfium2 as pdfium  # type: ignore[import-not-found,import-untyped,unused-ignore]
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise SheetToolingError("pypdfium2 is missing: uv sync --extra sheet") from exc
    document = pdfium.PdfDocument(sheet.data)
    try:
        page = document[0]
        return page.render(scale=RENDER_SCALE).to_pil().convert("RGB")
    finally:
        document.close()


@dataclass(frozen=True)
class Token:
    """One OCR text box: top-left corner and height in pixels, text, confidence."""

    x: float
    y: float
    h: float
    text: str
    conf: float


_ENGINE: Any = None


def ocr(image: Any) -> list[Token]:
    """Read every text box on the image."""
    global _ENGINE
    if _ENGINE is None:
        try:
            from rapidocr_onnxruntime import (  # type: ignore[import-not-found,import-untyped,unused-ignore]
                RapidOCR,
            )
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise SheetToolingError(
                "rapidocr-onnxruntime is missing: uv sync --extra sheet"
            ) from exc
        _ENGINE = RapidOCR()
    import numpy as np

    result, _timing = _ENGINE(np.asarray(image))
    return [
        Token(
            x=float(box[0][0]),
            y=float(box[0][1]),
            h=float(box[2][1]) - float(box[0][1]),
            text=str(text),
            conf=float(conf),
        )
        for box, text, conf in (result or [])
    ]


# --- parsing (pure) --------------------------------------------------------------

# "+3", "-8½", "+½", and what OCR makes of the ½ glyph ("%", "y", "1/2").
_SPREAD_RE = re.compile(r"^([+-])\s*(\d{1,2})?\s*(½|%|1/2|y|Y)?$")
_PICKEM = {"PK", "PICK", "EV", "EVEN"}
# Digit-for-letter confusions in the sheet's condensed face.
_LETTER_FIXES = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B"})
_ROW_TOLERANCE = 0.6  # of the spread glyphs' own height; rows sit ~1.2 heights apart


def parse_spread(text: str) -> float | None:
    """A sheet spread string as a signed number, or None if it isn't one."""
    compact = text.strip().replace(" ", "")
    if compact.upper() in _PICKEM:
        return 0.0
    match = _SPREAD_RE.match(compact)
    if not match:
        return None
    sign, whole, half = match.groups()
    if whole is None and half is None:
        return None
    value = int(whole or 0) + (0.5 if half else 0.0)
    return -value if sign == "-" else value


def resolve_team(text: str, candidates: dict[str, str]) -> str | None:
    """Team code for a sheet text box, or None.

    `candidates` maps NICKNAME -> code for the teams that can appear. The
    last word is matched by nickname suffix (rotation numbers and dates are
    glued on the left: "22CARDINALS", "449ERS"), with digit-for-letter fixes
    ("D0LPHINS"), then a close-match fallback for truncations."""
    words = text.strip().upper().split()
    if not words:
        return None
    word = words[-1]
    fixed = word.translate(_LETTER_FIXES)
    hits = [
        (len(nick), code)
        for nick, code in candidates.items()
        if word.endswith(nick) or fixed.endswith(nick)
    ]
    if hits:
        return max(hits)[1]
    stripped = re.sub(r"^\d+", "", fixed)
    close = difflib.get_close_matches(stripped, list(candidates), n=1, cutoff=0.8)
    return candidates[close[0]] if close else None


def nicknames_for(games: Sequence[Game]) -> dict[str, str]:
    """NICKNAME -> code for every team in `games` (aliases included)."""
    playing = {g.home_team for g in games} | {g.away_team for g in games}
    result = {nick: code for nick, code in teams.nfl_nicknames().items() if code in playing}
    result.update({nick: code for nick, code in NICKNAME_ALIASES.items() if code in playing})
    return result


@dataclass(frozen=True)
class Reading:
    """One (team, spread) pair as read off the sheet, before validation."""

    team: str
    spread: float
    text: str  # the team text box, for diagnostics


@dataclass(frozen=True)
class SheetLine:
    game_id: str
    away_team: str
    home_team: str
    home_spread: float


@dataclass(frozen=True)
class SheetParse:
    lines: list[SheetLine]
    problems: list[str] = field(default_factory=list)
    readings: list[Reading] = field(default_factory=list)


def parse_tokens(tokens: Sequence[Token], games: Sequence[Game]) -> SheetParse:
    """Turn OCR tokens into validated contest lines for the week's games.

    Spreads are the anchors: each pairs with the nearest text box to its
    left on the same row, "same row" being measured in the spread glyphs'
    own height so image size and layout never matter. Teams resolve among
    the week's schedule only. A game is accepted when both sides were read
    and mirror each other."""
    candidates = nicknames_for(games)
    spreads: list[tuple[Token, float]] = []
    others: list[Token] = []
    for token in tokens:
        value = parse_spread(token.text)
        if value is None:
            others.append(token)
        else:
            spreads.append((token, value))
    tolerance = _ROW_TOLERANCE * (median(t.h for t, _v in spreads) if spreads else 0.0)

    readings: list[Reading] = []
    problems: list[str] = []
    for spread_token, value in spreads:
        left = [
            t for t in others if t.x < spread_token.x and abs(t.y - spread_token.y) <= tolerance
        ]
        if not left:
            problems.append(f"spread {spread_token.text!r} has no team text on its row")
            continue
        partner = max(left, key=lambda t: t.x)
        code = resolve_team(partner.text, candidates)
        if code is None:
            problems.append(
                f"could not match {partner.text!r} (next to {spread_token.text!r}) to a team"
                " playing this week"
            )
            continue
        readings.append(Reading(team=code, spread=value, text=partner.text))

    by_team: dict[str, list[Reading]] = {}
    for reading in readings:
        by_team.setdefault(reading.team, []).append(reading)
    for code, seen in by_team.items():
        if len(seen) > 1:
            problems.append(
                f"{code} read {len(seen)} times: "
                + ", ".join(f"{r.text!r} {r.spread:+g}" for r in seen)
            )

    lines: list[SheetLine] = []
    for game in sorted(games, key=lambda g: (g.start_time, g.game_id)):
        home = by_team.get(game.home_team, [])
        away = by_team.get(game.away_team, [])
        label = f"{game.away_team} @ {game.home_team}"
        if len(home) != 1 or len(away) != 1:
            if not home and not away:
                problems.append(f"{label}: not on the sheet")
            elif len(home) == 1 and not away:
                problems.append(f"{label}: only the home side read ({home[0].spread:+g})")
            elif len(away) == 1 and not home:
                problems.append(f"{label}: only the away side read ({away[0].spread:+g})")
            continue
        if home[0].spread != -away[0].spread:
            problems.append(
                f"{label}: sides don't mirror (home {home[0].spread:+g}, away {away[0].spread:+g})"
            )
            continue
        if abs(home[0].spread) > 30:
            problems.append(f"{label}: implausible spread {home[0].spread:+g}")
            continue
        lines.append(
            SheetLine(
                game_id=game.game_id,
                away_team=game.away_team,
                home_team=game.home_team,
                home_spread=home[0].spread,
            )
        )
    return SheetParse(lines=lines, problems=problems, readings=readings)


def read_sheet(sheet: Sheet, games: Sequence[Game]) -> SheetParse:
    """render -> ocr -> parse, for callers that have the optional extra."""
    return parse_tokens(ocr(render(sheet)), games)
