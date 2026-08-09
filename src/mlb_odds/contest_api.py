"""FastAPI app for the Circa Million consensus tool (C1: board + contest lines).

Run beside the MLB odds server: `uvicorn mlb_odds.contest_api:app`. Reads the
NFL odds database (read-only, same hardening rationale as api.py — the odds
file is collector-owned market history) and owns contest.sqlite for app state.
Both paths are deployment configuration, never request input.
"""

import logging
import os
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from mlb_odds import contest, matchup, model, valuation
from mlb_odds.providers.base import ProviderError
from mlb_odds.providers.espn import ESPN
from mlb_odds.storage import Storage

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Circa Million Consensus API",
    description="Weekly board: market spreads vs Circa contest lines",
)


def _resolve_nfl_db() -> Path:
    env = os.environ.get("NFL_ODDS_DB")
    return Path(env) if env else Path("./nfl-odds.sqlite")


def _resolve_contest_db() -> Path:
    env = os.environ.get("CONTEST_DB")
    return Path(env) if env else Path("./contest.sqlite")


def _members() -> list[str]:
    """The three-person group, ordered — order defines captain rotation."""
    raw = os.environ.get("CONTEST_MEMBERS", "player1,player2,player3")
    members = [m.strip() for m in raw.split(",") if m.strip()]
    if not members:
        raise HTTPException(status_code=500, detail="CONTEST_MEMBERS is empty")
    return members


def _now() -> datetime:
    """Injection seam for tests; everything time-dependent goes through it."""
    return datetime.now(UTC)


def _member_emails() -> dict[str, str]:
    """Cloudflare Access email -> member mapping (D-026). Empty when unset —
    the tailnet path needs no identity headers."""
    raw = os.environ.get("CONTEST_MEMBER_EMAILS", "")
    mapping = {}
    for pair in raw.split(","):
        email, _, member = pair.strip().partition(":")
        if email and member:
            mapping[email.lower()] = member.strip()
    return mapping


def _identity(request: Request) -> str | None:
    """The authenticated member, when the request came through Cloudflare
    Access. Trustworthy because nothing but the tunnel can reach this app
    from outside the tailnet (no published ports); tailnet requests carry no
    header and stay on the existing honor system among members."""
    email = request.headers.get("Cf-Access-Authenticated-User-Email")
    if not email:
        return None
    member = _member_emails().get(email.strip().lower())
    if member is None:
        raise HTTPException(
            status_code=403,
            detail=f"{email} passed Access but is not mapped in CONTEST_MEMBER_EMAILS",
        )
    return member


def _enforce_identity(request: Request, member: str) -> None:
    """A publicly-authenticated user may only act as themselves."""
    identified = _identity(request)
    if identified is not None and identified != member:
        raise HTTPException(
            status_code=403,
            detail=f"authenticated as {identified}; cannot act as {member}",
        )


def _require_member(member: str) -> str:
    if member not in _members():
        raise HTTPException(
            status_code=403, detail=f"unknown member {member!r}; set CONTEST_MEMBERS"
        )
    return member


def _require_submitted(store: contest.ContestStore, week: int, member: str) -> None:
    """The blind rule: nobody sees others' proposals before their own are in."""
    if not store.has_submitted(week, member):
        raise HTTPException(
            status_code=409,
            detail=f"{member} has not submitted week-{week} proposals yet — "
            "propose first, then the reveal unlocks.",
        )


def _open_odds() -> Storage:
    """NFL market data, read-only: this app must never create or migrate the
    collector's database (see Storage.__init__ / api.py `_resolve_db`)."""
    try:
        return Storage(_resolve_nfl_db(), read_only=True)
    except sqlite3.OperationalError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "NFL odds database unavailable. "
                "Run `mlb-odds collect --once --sport nfl` to create it."
            ),
        ) from exc


class ContestLineIn(BaseModel):
    week: int = Field(ge=1, le=contest.NUM_WEEKS)
    game_id: str
    home_spread: float

    @field_validator("home_spread")
    @classmethod
    def _plausible_half_point(cls, v: float) -> float:
        if abs(v) > 30:
            raise ValueError(f"implausible contest spread {v}")
        if (v * 2) != int(v * 2):
            raise ValueError(f"contest spread must be a multiple of 0.5, got {v}")
        return v


class ContestLineOut(BaseModel):
    week: int
    game_id: str
    home_spread: float
    entered_at: str


class BoardGameOut(BaseModel):
    game_id: str
    away_team: str
    home_team: str
    start_time: str  # Pacific ISO — the contest's timezone
    early_kickoff: bool  # kicks before Sat 4 PM: picking it pulls the card deadline in (Rule 8)
    books: dict[str, float]
    consensus: float | None
    contest_line: float | None
    line_entered_at: str | None
    edge: float | None  # contest_line - consensus; positive → value on home
    value_side: str | None
    key_numbers: list[float]
    movement_since_entry: float | None
    predicted_line: float | None  # market-implied power-rating model (C4.4)
    model_win_prob: float | None  # D-036 two-lens blend, home side
    ml_lens_prob: float | None
    spread_lens_prob: float | None
    home_rest: int | None  # days since each team's previous stored game (C4.5)
    away_rest: int | None
    rest_differential: int | None  # home_rest - away_rest; positive = home fresher
    divisional: bool


class BoardOut(BaseModel):
    week: int
    window_start: str
    window_end: str
    lines_post: str
    deadline: str
    seconds_to_deadline: int
    locked: bool  # past the week's Saturday 4 PM PT deadline
    captain: str
    card_locked: bool
    booby_guard_alert: bool  # Sat 10 AM PT passed with no locked card
    games: list[BoardGameOut]


def _pt(value: datetime) -> str:
    return value.astimezone(contest.PACIFIC).isoformat()


@app.get("/api/contest/board", response_model=BoardOut)
def get_board(week: int | None = None) -> BoardOut:
    """The weekly board: market context + contest lines + countdown.

    `week` defaults to the contest week containing now; outside the season a
    week must be passed explicitly.
    """
    now = _now()
    if week is None:
        week = contest.week_of(now)
        if week is None:
            raise HTTPException(
                status_code=400,
                detail="Now is outside contest Weeks 1-18; pass ?week= explicitly.",
            )
    if not 1 <= week <= contest.NUM_WEEKS:
        raise HTTPException(status_code=422, detail=f"week must be 1-{contest.NUM_WEEKS}")

    odds = _open_odds()
    store = contest.ContestStore(_resolve_contest_db())
    try:
        rows = contest.build_board(odds, store.lines(week), week)
        card = store.card(week)
        all_games = odds.games()
        fitted = contest.power_ratings(odds)
        fitted_ml = valuation.implied_strengths(odds)
        contexts = {
            row.game_id: contest.game_context(all_games, game)
            for row in rows
            for game in (g for g in all_games if g.game_id == row.game_id)
        }
    finally:
        store.close()
        odds.close()
    ratings, hfa = fitted if fitted else ({}, 0.0)
    ml_strengths, ml_hfa = fitted_ml if fitted_ml else ({}, None)

    def _row_model(home: str, away: str) -> tuple[float | None, float | None, float | None]:
        """(blended win prob, ml lens, spread lens) for one matchup (D-036)."""
        ml = (
            valuation.model_home_prob(ml_strengths, ml_hfa, home, away)
            if ml_hfa is not None
            else None
        )
        line = contest.predicted_home_spread(ratings, hfa, home, away)
        blended, spread_lens = model.nfl_model_prob(ml, line)
        return blended, ml, spread_lens

    row_models = {row.game_id: _row_model(row.home_team, row.away_team) for row in rows}

    start, end = contest.week_window(week)
    deadline = contest.pick_deadline(week)
    return BoardOut(
        week=week,
        window_start=_pt(start),
        window_end=_pt(end),
        lines_post=_pt(contest.lines_post_time(week)),
        deadline=_pt(deadline),
        seconds_to_deadline=int((deadline - now).total_seconds()),
        locked=now >= deadline,
        captain=contest.captain_for(week, _members()),
        card_locked=card is not None,
        booby_guard_alert=contest.booby_guard_alert(
            week, card_locked=card is not None, now=now
        ),
        games=[
            BoardGameOut(
                game_id=row.game_id,
                away_team=row.away_team,
                home_team=row.home_team,
                start_time=_pt(row.start_time),
                early_kickoff=row.start_time < deadline,
                books=row.books,
                consensus=row.consensus,
                contest_line=row.contest_line,
                line_entered_at=(
                    _pt(row.line_entered_at) if row.line_entered_at is not None else None
                ),
                edge=row.edge,
                value_side=row.value_side,
                key_numbers=row.key_numbers,
                movement_since_entry=row.movement_since_entry,
                predicted_line=contest.predicted_home_spread(
                    ratings, hfa, row.home_team, row.away_team
                ),
                model_win_prob=row_models[row.game_id][0],
                ml_lens_prob=row_models[row.game_id][1],
                spread_lens_prob=row_models[row.game_id][2],
                home_rest=(
                    contexts[row.game_id].home_rest if row.game_id in contexts else None
                ),
                away_rest=(
                    contexts[row.game_id].away_rest if row.game_id in contexts else None
                ),
                rest_differential=(
                    contexts[row.game_id].rest_differential
                    if row.game_id in contexts
                    else None
                ),
                divisional=(
                    contexts[row.game_id].divisional if row.game_id in contexts else False
                ),
            )
            for row in sorted(rows, key=lambda r: (r.start_time, r.game_id))
        ],
    )


@app.post("/api/contest/lines", response_model=ContestLineOut, status_code=201)
def set_contest_line(body: ContestLineIn) -> ContestLineOut:
    """Enter (or correct) one manually read Circa contest line.

    The game must already exist in the odds database inside that contest
    week's window — rejecting unknown ids here keeps a typo from becoming a
    phantom row that never joins to the board.
    """
    odds = _open_odds()
    try:
        known = {g.game_id for g in odds.games(window=contest.week_window(body.week))}
    finally:
        odds.close()
    if body.game_id not in known:
        raise HTTPException(
            status_code=404,
            detail=(
                f"{body.game_id} is not a stored NFL game in contest week {body.week}. "
                "Collect NFL odds first; the board shows valid game_ids."
            ),
        )

    entered_at = _now()
    store = contest.ContestStore(_resolve_contest_db())
    try:
        store.set_line(body.week, body.game_id, body.home_spread, entered_at=entered_at)
    finally:
        store.close()
    logger.info(
        "contest line set: week %d %s home %+.1f", body.week, body.game_id, body.home_spread
    )
    return ContestLineOut(
        week=body.week,
        game_id=body.game_id,
        home_spread=body.home_spread,
        entered_at=_pt(entered_at),
    )


@app.get("/api/contest/lines", response_model=list[ContestLineOut])
def get_contest_lines(week: int) -> list[ContestLineOut]:
    """All entered contest lines for a week."""
    if not 1 <= week <= contest.NUM_WEEKS:
        raise HTTPException(status_code=422, detail=f"week must be 1-{contest.NUM_WEEKS}")
    store = contest.ContestStore(_resolve_contest_db())
    try:
        lines = store.lines(week)
    finally:
        store.close()
    return [
        ContestLineOut(
            week=line.week,
            game_id=line.game_id,
            home_spread=line.home_spread,
            entered_at=_pt(line.entered_at),
        )
        for line in sorted(lines.values(), key=lambda li: li.game_id)
    ]


Side = Literal["home", "away"]
StanceSide = Literal["home", "away", "pass"]  # proposals/votes may pass (D-033)


class MembersOut(BaseModel):
    members: list[str]
    captains: dict[int, str]  # week -> captain


@app.get("/api/contest/members", response_model=MembersOut)
def get_members() -> MembersOut:
    members = _members()
    return MembersOut(
        members=members,
        captains={
            w: contest.captain_for(w, members) for w in range(1, contest.NUM_WEEKS + 1)
        },
    )


class ProposalPickIn(BaseModel):
    game_id: str
    side: StanceSide
    note: str = ""


class ProposalsIn(BaseModel):
    week: int = Field(ge=1, le=contest.NUM_WEEKS)
    member: str
    picks: list[ProposalPickIn] = Field(min_length=1, max_length=20)


class ProposalOut(BaseModel):
    member: str
    game_id: str
    side: str
    note: str


class ProposalsOut(BaseModel):
    week: int
    submitted: list[str]  # members whose blind sets are in
    waiting_on: list[str]
    proposals: list[ProposalOut]  # own always; everyone's once you've submitted


@app.post("/api/contest/proposals", response_model=ProposalsOut, status_code=201)
def submit_proposals(body: ProposalsIn, request: Request) -> ProposalsOut:
    """A member's blind proposal set: 1-5 picks, one shot, immutable.

    Immutability is what makes the blind phase honest — you cannot peek at
    the reveal and then edit.
    """
    _require_member(body.member)
    _enforce_identity(request, body.member)
    odds = _open_odds()
    try:
        known = {g.game_id for g in odds.games(window=contest.week_window(body.week))}
    finally:
        odds.close()
    unknown = [p.game_id for p in body.picks if p.game_id not in known]
    if unknown:
        raise HTTPException(
            status_code=404, detail=f"not stored NFL games in week {body.week}: {unknown}"
        )
    store = contest.ContestStore(_resolve_contest_db())
    try:
        try:
            store.submit_proposals(
                body.week,
                body.member,
                [(p.game_id, p.side, p.note) for p in body.picks],
                submitted_at=_now(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _proposals_view(store, body.week, body.member)
    finally:
        store.close()


@app.get("/api/contest/proposals", response_model=ProposalsOut)
def get_proposals(week: int, member: str, request: Request) -> ProposalsOut:
    """Own proposals always; the whole group's only after yours are submitted."""
    _require_member(member)
    _enforce_identity(request, member)
    store = contest.ContestStore(_resolve_contest_db())
    try:
        return _proposals_view(store, week, member)
    finally:
        store.close()


def _proposals_view(store: contest.ContestStore, week: int, member: str) -> ProposalsOut:
    submitted = store.submitted_members(week)
    mine_only = member not in submitted
    rows = store.proposals(week, member=member if mine_only else None)
    return ProposalsOut(
        week=week,
        submitted=submitted,
        waiting_on=[m for m in _members() if m not in submitted],
        proposals=[
            ProposalOut(member=p.member, game_id=p.game_id, side=p.side, note=p.note)
            for p in rows
        ],
    )


class VoteIn(BaseModel):
    week: int = Field(ge=1, le=contest.NUM_WEEKS)
    member: str
    game_id: str
    side: StanceSide


class CandidateOut(BaseModel):
    game_id: str
    side: str
    backers: list[str]
    status: str  # unanimous | majority | contested


class ConsensusOut(BaseModel):
    week: int
    captain: str
    passes: dict[str, list[str]]  # game_id -> members explicitly passing (D-033)
    candidates: list[CandidateOut]
    working_card: list[CandidateOut]  # top 5 by backing — what would lock now
    effective_deadline: str  # Rule 8: pulled to earliest kickoff on the working card
    deadline_pulled_forward_by: str | None  # game_id responsible, if any
    card_locked: bool


@app.post("/api/contest/votes", response_model=ConsensusOut)
def cast_vote(body: VoteIn, request: Request) -> ConsensusOut:
    """Vote (or change your stance) on one game. Requires your proposals in —
    voting is part of the reveal phase."""
    _require_member(body.member)
    _enforce_identity(request, body.member)
    store = contest.ContestStore(_resolve_contest_db())
    try:
        _require_submitted(store, body.week, body.member)
        if store.card(body.week) is not None:
            raise HTTPException(
                status_code=409, detail=f"week {body.week} card is locked; voting is over"
            )
        store.cast_vote(body.week, body.member, body.game_id, body.side, cast_at=_now())
        return _consensus_view(store, body.week)
    finally:
        store.close()


@app.get("/api/contest/consensus", response_model=ConsensusOut)
def get_consensus(week: int, member: str, request: Request) -> ConsensusOut:
    """The reveal: everyone's stances tallied. Blind rule applies."""
    _require_member(member)
    _enforce_identity(request, member)
    store = contest.ContestStore(_resolve_contest_db())
    try:
        _require_submitted(store, week, member)
        return _consensus_view(store, week)
    finally:
        store.close()


def _consensus_view(store: contest.ContestStore, week: int) -> ConsensusOut:
    candidates = contest.tally_candidates(
        store.proposals(week), store.votes(week), _members()
    )
    working = candidates[:5]
    kickoffs = _kickoffs(week, [c.game_id for c in working])
    deadline = contest.effective_deadline(week, kickoffs.values())
    pulled_by = None
    if deadline < contest.pick_deadline(week):
        pulled_by = min(kickoffs, key=lambda g: kickoffs[g])
    return ConsensusOut(
        week=week,
        captain=contest.captain_for(week, _members()),
        passes=contest.passes_by_game(store.proposals(week), store.votes(week)),
        candidates=[
            CandidateOut(
                game_id=c.game_id, side=c.side, backers=list(c.backers), status=c.status
            )
            for c in candidates
        ],
        working_card=[
            CandidateOut(
                game_id=c.game_id, side=c.side, backers=list(c.backers), status=c.status
            )
            for c in working
        ],
        effective_deadline=_pt(deadline),
        deadline_pulled_forward_by=pulled_by,
        card_locked=store.card(week) is not None,
    )


def _kickoffs(week: int, game_ids: list[str]) -> dict[str, datetime]:
    odds = _open_odds()
    try:
        return {
            g.game_id: g.start_time
            for g in odds.games(window=contest.week_window(week))
            if g.game_id in game_ids
        }
    finally:
        odds.close()


class CardPickIn(BaseModel):
    game_id: str
    side: Side


class CardIn(BaseModel):
    week: int = Field(ge=1, le=contest.NUM_WEEKS)
    member: str
    picks: list[CardPickIn] = Field(min_length=5, max_length=5)


class CardPickOut(BaseModel):
    game_id: str
    side: str
    result: str | None


class CardOut(BaseModel):
    week: int
    picks: list[CardPickOut]
    locked_by: str
    locked_at: str
    etsn: str | None
    effective_deadline: str


@app.post("/api/contest/card", response_model=CardOut, status_code=201)
def lock_card(body: CardIn, request: Request) -> CardOut:
    """Lock the week's official five. Enforces Rule 8: if any pick kicks off
    before Saturday 4 PM PT, the whole card is due before that kickoff."""
    _require_member(body.member)
    _enforce_identity(request, body.member)
    picks = [(p.game_id, p.side) for p in body.picks]
    kickoffs = _kickoffs(body.week, [g for g, _ in picks])
    missing = [g for g, _ in picks if g not in kickoffs]
    if missing:
        raise HTTPException(
            status_code=404, detail=f"not stored NFL games in week {body.week}: {missing}"
        )
    deadline = contest.effective_deadline(body.week, kickoffs.values())
    now = _now()
    if now >= deadline:
        raise HTTPException(
            status_code=409,
            detail=f"past this card's effective deadline ({_pt(deadline)}) — "
            "Rule 8 pulls the deadline to the earliest selected kickoff.",
        )
    store = contest.ContestStore(_resolve_contest_db())
    try:
        try:
            store.lock_card(body.week, picks, locked_by=body.member, locked_at=now)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        card = store.card(body.week)
    finally:
        store.close()
    assert card is not None
    logger.info("week %d card locked by %s: %s", body.week, body.member, picks)
    return _card_out(card, deadline)


class EtsnIn(BaseModel):
    week: int = Field(ge=1, le=contest.NUM_WEEKS)
    etsn: str = Field(min_length=1, max_length=40)


@app.patch("/api/contest/card", response_model=CardOut)
def record_etsn(body: EtsnIn) -> CardOut:
    """Attach Circa's confirmation (the 12-digit ETSN) to the locked card —
    proof the card actually made it into the contest."""
    store = contest.ContestStore(_resolve_contest_db())
    try:
        try:
            store.set_etsn(body.week, body.etsn)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        card = store.card(body.week)
    finally:
        store.close()
    assert card is not None
    return _card_out(card, _card_deadline(card))


@app.get("/api/contest/card", response_model=CardOut)
def get_card(week: int) -> CardOut:
    store = contest.ContestStore(_resolve_contest_db())
    try:
        card = store.card(week)
    finally:
        store.close()
    if card is None:
        raise HTTPException(status_code=404, detail=f"no locked card for week {week}")
    return _card_out(card, _card_deadline(card))


def _card_deadline(card: contest.Card) -> datetime:
    kickoffs = _kickoffs(card.week, [p.game_id for p in card.picks])
    return contest.effective_deadline(card.week, kickoffs.values())


def _card_out(card: contest.Card, deadline: datetime) -> CardOut:
    return CardOut(
        week=card.week,
        picks=[
            CardPickOut(game_id=p.game_id, side=p.side, result=p.result)
            for p in card.picks
        ],
        locked_by=card.locked_by,
        locked_at=_pt(card.locked_at),
        etsn=card.etsn,
        effective_deadline=_pt(deadline),
    )


class ResultsIn(BaseModel):
    week: int = Field(ge=1, le=contest.NUM_WEEKS)
    results: dict[str, Literal["win", "loss", "push"]]


@app.post("/api/contest/results", response_model=CardOut)
def record_results(body: ResultsIn) -> CardOut:
    """Grade card picks after games finish (re-entry corrects)."""
    store = contest.ContestStore(_resolve_contest_db())
    try:
        try:
            store.record_results(body.week, dict(body.results))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        card = store.card(body.week)
    finally:
        store.close()
    assert card is not None
    return _card_out(card, _card_deadline(card))


class WeekScoreOut(BaseModel):
    week: int
    wins: int
    losses: int
    pushes: int
    points: float
    graded: int
    picks: int


class LadderOut(BaseModel):
    total_wins: int
    winning_weeks: int
    weeks_5_0: int
    weeks_4_0_1: int
    weeks_4_1: int


class SeasonOut(BaseModel):
    total_points: float
    weeks: list[WeekScoreOut]
    tiebreakers: LadderOut
    quarters: dict[int, float]
    booby_eligible: bool


@app.get("/api/contest/season", response_model=SeasonOut)
def get_season() -> SeasonOut:
    """Season standing: points, the 1st-place tiebreaker ladder, quarter
    totals, and booby-prize eligibility."""
    store = contest.ContestStore(_resolve_contest_db())
    try:
        cards = store.all_cards()
    finally:
        store.close()
    scores, ladder, quarters, booby = contest.season_summary(cards, now=_now())
    return SeasonOut(
        total_points=sum(s.points for s in scores),
        weeks=[
            WeekScoreOut(
                week=s.week,
                wins=s.wins,
                losses=s.losses,
                pushes=s.pushes,
                points=s.points,
                graded=s.graded,
                picks=s.picks,
            )
            for s in scores
        ],
        tiebreakers=LadderOut(
            total_wins=ladder.total_wins,
            winning_weeks=ladder.winning_weeks,
            weeks_5_0=ladder.weeks_5_0,
            weeks_4_0_1=ladder.weeks_4_0_1,
            weeks_4_1=ladder.weeks_4_1,
        ),
        quarters=quarters,
        booby_eligible=booby,
    )


def _finals_source() -> ESPN:
    """Constructor seam (monkeypatched in tests): the ESPN scoreboard is the
    free finals source. Built per request — this app holds no live handles."""
    return ESPN(sport="nfl")


class AutoGradeSkip(BaseModel):
    game_id: str
    reason: str


class AutoGradeOut(BaseModel):
    week: int
    graded: dict[str, str]  # game_id -> win/loss/push written this call
    skipped: list[AutoGradeSkip]
    card: CardOut


@app.post("/api/contest/results/auto", response_model=AutoGradeOut)
def auto_grade(week: int) -> AutoGradeOut:
    """Grade the week's card from ESPN final scores against the stored Circa
    contest lines. Free (ESPN is unmetered); safe to re-run — regrading a
    corrected score overwrites, and non-final games are skipped with reasons.
    """
    store = contest.ContestStore(_resolve_contest_db())
    try:
        card = store.card(week)
        if card is None:
            raise HTTPException(status_code=404, detail=f"no locked card for week {week}")
        lines = store.lines(week)

        odds = _open_odds()
        try:
            games = {
                g.game_id: g for g in odds.games(window=contest.week_window(week))
            }
        finally:
            odds.close()

        eastern = ZoneInfo("America/New_York")  # ESPN groups scoreboard days in ET
        pick_games = [games[p.game_id] for p in card.picks if p.game_id in games]
        days = {g.start_time.astimezone(eastern).date() for g in pick_games}
        source = _finals_source()
        finals = {}
        try:
            for day in sorted(days):
                for final in source.fetch_final_scores(day):
                    finals[(final.away_team, final.home_team)] = final
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=f"finals source failed: {exc}") from exc

        graded: dict[str, str] = {}
        skipped: list[AutoGradeSkip] = []
        for pick in card.picks:
            game = games.get(pick.game_id)
            if game is None:
                skipped.append(
                    AutoGradeSkip(game_id=pick.game_id, reason="not in odds database")
                )
                continue
            line = lines.get(pick.game_id)
            if line is None:
                skipped.append(
                    AutoGradeSkip(game_id=pick.game_id, reason="no contest line entered")
                )
                continue
            found = finals.get((game.away_team, game.home_team))
            if found is None:
                skipped.append(
                    AutoGradeSkip(game_id=pick.game_id, reason="no final score found")
                )
                continue
            final = found
            if not final.completed:
                skipped.append(AutoGradeSkip(game_id=pick.game_id, reason="game not final"))
                continue
            graded[pick.game_id] = contest.grade_pick(
                pick.side, line.home_spread, final.home_score, final.away_score
            )

        if graded:
            store.record_results(week, graded)
            logger.info("week %d auto-graded %d pick(s): %s", week, len(graded), graded)
        card = store.card(week)
    finally:
        store.close()
    assert card is not None
    return AutoGradeOut(
        week=week,
        graded=graded,
        skipped=skipped,
        card=_card_out(card, _card_deadline(card)),
    )


class SpreadTickOut(BaseModel):
    t: str  # Pacific ISO
    book: str
    spread: float


class ConsensusPointOut(BaseModel):
    t: str
    spread: float


class SpreadHistoryOut(BaseModel):
    game_id: str
    away_team: str
    home_team: str
    start_time: str
    week: int | None
    lines_post: str | None
    deadline: str | None
    contest_line: float | None
    line_entered_at: str | None
    model_line: float | None  # current power-rating prediction (C4.4) — a
    # reference value, not a series: historical rating fits are not stored
    books: list[SpreadTickOut]  # every stored home-spread observation
    consensus: list[ConsensusPointOut]  # carry-forward median at each snapshot time


@app.get("/api/contest/games/{game_id}/spread-history", response_model=SpreadHistoryOut)
def get_spread_history(game_id: str) -> SpreadHistoryOut:
    """Line movement for one game, chart-ready: raw per-book ticks plus the
    carry-forward consensus series (same as-of semantics the edge math uses,
    so the chart and the board can never disagree)."""
    try:
        game_date = date.fromisoformat(game_id[:10])
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"malformed game_id {game_id!r}") from exc
    odds = _open_odds()
    try:
        game = next((g for g in odds.games(game_date) if g.game_id == game_id), None)
        if game is None:
            raise HTTPException(status_code=404, detail=f"unknown game {game_id}")
        ticks = contest.spread_history(odds, game_id)
        fitted = contest.power_ratings(odds)
    finally:
        odds.close()
    model_line = None
    if fitted is not None:
        model_line = contest.predicted_home_spread(
            fitted[0], fitted[1], game.home_team, game.away_team
        )

    week = contest.week_of(game.start_time)
    line = None
    if week is not None:
        store = contest.ContestStore(_resolve_contest_db())
        try:
            line = store.lines(week).get(game_id)
        finally:
            store.close()

    times = sorted({t.fetched_at for t in ticks})
    consensus_series = []
    for t in times:
        value = contest.consensus(contest.book_spreads(ticks, asof=t))
        if value is not None:
            consensus_series.append(ConsensusPointOut(t=_pt(t), spread=value))
    return SpreadHistoryOut(
        game_id=game_id,
        away_team=game.away_team,
        home_team=game.home_team,
        start_time=_pt(game.start_time),
        week=week,
        lines_post=_pt(contest.lines_post_time(week)) if week else None,
        deadline=_pt(contest.pick_deadline(week)) if week else None,
        contest_line=line.home_spread if line else None,
        line_entered_at=_pt(line.entered_at) if line else None,
        model_line=model_line,
        books=[
            SpreadTickOut(t=_pt(t.fetched_at), book=t.book, spread=t.home_spread)
            for t in ticks
        ],
        consensus=consensus_series,
    )


class ClvPickOut(BaseModel):
    week: int
    game_id: str
    side: str
    contest_line: float
    closing: float | None
    clv: float | None
    result: str | None


class ClvOut(BaseModel):
    picks: list[ClvPickOut]
    n: int  # picks with a computable CLV
    total_clv: float
    avg_clv: float | None
    positive: int
    negative: int


@app.get("/api/contest/stats/clv", response_model=ClvOut)
def get_clv() -> ClvOut:
    """Closing line value per locked pick (C4.2) — the north-star process
    metric: positive means the contest number beat the market close."""
    odds = _open_odds()
    store = contest.ContestStore(_resolve_contest_db())
    try:
        rows = contest.clv_report(odds, store)
    finally:
        store.close()
        odds.close()
    scored = [r.clv for r in rows if r.clv is not None]
    return ClvOut(
        picks=[
            ClvPickOut(
                week=r.week, game_id=r.game_id, side=r.side,
                contest_line=r.contest_line, closing=r.closing, clv=r.clv,
                result=r.result,
            )
            for r in rows
        ],
        n=len(scored),
        total_clv=round(sum(scored), 2),
        avg_clv=round(sum(scored) / len(scored), 3) if scored else None,
        positive=sum(1 for c in scored if c > 0),
        negative=sum(1 for c in scored if c < 0),
    )


class CalibrationBucketOut(BaseModel):
    label: str
    n: int
    wins: int
    losses: int
    pushes: int
    cover_rate: float | None


@app.get("/api/contest/stats/calibration", response_model=list[CalibrationBucketOut])
def get_calibration() -> list[CalibrationBucketOut]:
    """Cover rate by at-lock edge bucket (C4.3): does the edge signal predict?"""
    odds = _open_odds()
    store = contest.ContestStore(_resolve_contest_db())
    try:
        buckets = contest.calibration_report(odds, store)
    finally:
        store.close()
        odds.close()
    return [
        CalibrationBucketOut(
            label=b.label, n=b.n, wins=b.wins, losses=b.losses, pushes=b.pushes,
            cover_rate=b.cover_rate,
        )
        for b in buckets
    ]


class RatingOut(BaseModel):
    team: str
    rating: float  # points vs league average on a neutral field


class RatingsOut(BaseModel):
    hfa: float
    n_teams: int
    ratings: list[RatingOut]  # best first


@app.get("/api/contest/stats/ratings", response_model=RatingsOut)
def get_ratings() -> RatingsOut:
    """Market-implied power ratings (C4.4), fit from every stored spread."""
    odds = _open_odds()
    try:
        fitted = contest.power_ratings(odds)
    finally:
        odds.close()
    if fitted is None:
        raise HTTPException(status_code=404, detail="not enough stored spreads to fit")
    ratings, hfa = fitted
    ranked = sorted(ratings.items(), key=lambda kv: -kv[1])
    return RatingsOut(
        hfa=hfa,
        n_teams=len(ranked),
        ratings=[RatingOut(team=t, rating=r) for t, r in ranked],
    )


class MemberStatsOut(BaseModel):
    member: str
    proposal_record: str  # "W-L-P"
    stance_record: str
    captain_weeks: int
    captain_points: float


@app.get("/api/contest/stats/members", response_model=list[MemberStatsOut])
def get_member_stats() -> list[MemberStatsOut]:
    """Per-member records over graded picks (C4.5): whose opinion to weight."""
    store = contest.ContestStore(_resolve_contest_db())
    try:
        stats = contest.member_stats(store, _members())
    finally:
        store.close()
    return [
        MemberStatsOut(
            member=s.member,
            proposal_record=f"{s.proposal_wins}-{s.proposal_losses}-{s.proposal_pushes}",
            stance_record=f"{s.stance_wins}-{s.stance_losses}-{s.stance_pushes}",
            captain_weeks=s.captain_weeks,
            captain_points=s.captain_points,
        )
        for s in stats
    ]


class WhoamiOut(BaseModel):
    email: str | None  # Cloudflare Access identity, if any
    member: str | None  # mapped contest member; null on the tailnet path


@app.get("/api/contest/whoami", response_model=WhoamiOut)
def whoami(request: Request) -> WhoamiOut:
    """Who Access says you are. Tailnet requests (no header) get nulls and
    the UI keeps its member dropdown; mapped public users are locked to
    their identity."""
    email = request.headers.get("Cf-Access-Authenticated-User-Email")
    member = None
    if email:
        member = _member_emails().get(email.strip().lower())
    return WhoamiOut(email=email, member=member)


@app.get("/api/contest/games/{game_id}/matchup")
def get_matchup(game_id: str) -> dict[str, object]:
    """Head-to-head ESPN team lens for a contest game (D-034) — the same
    shared implementation as the odds app, NFL database."""
    try:
        game_date = date.fromisoformat(game_id[:10])
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"malformed game_id {game_id!r}") from exc
    odds = _open_odds()
    try:
        game = next((g for g in odds.games(game_date) if g.game_id == game_id), None)
    finally:
        odds.close()
    if game is None:
        raise HTTPException(status_code=404, detail=f"unknown game {game_id}")
    try:
        payload = matchup.matchup_payload(
            ESPN(sport="nfl"), "nfl", game.away_team, game.home_team
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=f"ESPN fetch failed: {exc}") from exc
    if payload is None:
        raise HTTPException(status_code=502, detail="ESPN team id missing")
    return {"game_id": game_id, **payload}


@app.get("/api/contest/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# Survivor endpoints (/api/survivor/*) share this app, its identity layer,
# and contest.sqlite. Imported at the bottom on purpose: survivor_api imports
# this module back for the shared helpers, and by this point every one of
# them exists (D-028).
from mlb_odds.survivor_api import router as _survivor_router  # noqa: E402

app.include_router(_survivor_router)

# Static contest UI at / — registered last so it cannot shadow API routes
# (same registration-order lesson as api.py's frontend mount).
_STATIC_DIR = Path(__file__).parent / "contest_static"
if _STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")
