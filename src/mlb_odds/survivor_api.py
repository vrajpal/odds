"""Survivor endpoints for the contest app (mounted under /api/survivor).

Rides the same FastAPI process, contest.sqlite file, identity layer, and
read-only NFL odds handling as the Million endpoints. `contest_api` includes
this router at the bottom of its module, so importing it back here as a module
object is safe: every helper this file calls exists by the time any request
runs (and the tests' monkeypatching of contest_api._now etc. reaches these
routes for free).
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from mlb_odds import contest, contest_api, model, survivor, valuation
from mlb_odds.models import Game
from mlb_odds.providers.base import ProviderError
from mlb_odds.teams import NFL_CODES, NFL_DIVISIONS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/survivor")


def _store() -> survivor.SurvivorStore:
    return survivor.SurvivorStore(contest_api._resolve_contest_db())


def _pt(value: datetime) -> str:
    return value.astimezone(survivor.PACIFIC).isoformat()


def _leg_or_422(leg_id: str) -> survivor.Leg:
    try:
        return survivor.leg(leg_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _leg_games(leg_: survivor.Leg) -> dict[str, Game]:
    odds = contest_api._open_odds()
    try:
        return {
            g.game_id: g for g in odds.games(window=(leg_.start, leg_.end))
        }
    finally:
        odds.close()


def _game_for_team(leg_: survivor.Leg, team: str) -> Game:
    """The stored game `team` plays in this leg — the schedule of record.

    Validating against stored games (not the rules' hardcoded holiday slates)
    means an NFL schedule change flows in with the next collect; it also means
    picks can't be made for legs whose games haven't been collected yet."""
    games = _leg_games(leg_)
    matches = [g for g in games.values() if team in (g.home_team, g.away_team)]
    if not matches:
        raise HTTPException(
            status_code=404,
            detail=(
                f"{team} has no stored game in the {leg_.label} window — either it"
                " doesn't play this leg, or NFL odds haven't been collected"
                " (`mlb-odds collect --once --sport nfl`)."
            ),
        )
    return min(matches, key=lambda g: g.start_time)


class PickOut(BaseModel):
    leg_id: str
    team: str
    game_id: str
    locked_by: str
    locked_at: str
    etsn: str | None
    result: str | None
    effective_deadline: str | None  # min(leg deadline, kickoff); None if game unknown


class LegOut(BaseModel):
    leg_id: str
    label: str
    start: str
    end: str
    opens: str
    deadline: str
    holiday_slate: list[str]  # rules-defined eligible teams; empty = open slate
    pick: PickOut | None


class HolidayOutlookOut(BaseModel):
    leg_id: str
    label: str
    picked: bool
    remaining: list[str]
    danger: str  # none | caution | critical | fatal


class EntryStatusOut(BaseModel):
    alive: bool
    survived: int
    reason: str | None
    at_leg: str | None


class StatusOut(BaseModel):
    current_leg: str | None
    entry: EntryStatusOut
    used: dict[str, str]  # team -> leg burned in
    remaining_teams: list[str]
    holiday_outlook: list[HolidayOutlookOut]
    legs: list[LegOut]


@router.get("/status", response_model=StatusOut)
def get_status() -> StatusOut:
    """The season at a glance: entry life, burned teams, and how exposed the
    entry is to the two holiday-slate traps (Rules 8/9)."""
    now = contest_api._now()
    store = _store()
    try:
        picks = store.all_picks()
        used = store.used_teams()
    finally:
        store.close()
    current = survivor.leg_for(now)
    status = survivor.entry_status(picks, now=now)
    return StatusOut(
        current_leg=current.leg_id if current else None,
        entry=EntryStatusOut(
            alive=status.alive,
            survived=status.survived,
            reason=status.reason,
            at_leg=status.at_leg,
        ),
        used=used,
        remaining_teams=sorted(NFL_CODES - set(used)),
        holiday_outlook=[
            HolidayOutlookOut(
                leg_id=o.leg_id,
                label=o.label,
                picked=o.picked,
                remaining=list(o.remaining),
                danger=o.danger,
            )
            for o in survivor.holiday_outlook(used, picks)
        ],
        legs=[_leg_out(leg_, picks.get(leg_.leg_id)) for leg_ in survivor.LEGS],
    )


def _leg_out(leg_: survivor.Leg, pick: survivor.SurvivorPick | None) -> LegOut:
    return LegOut(
        leg_id=leg_.leg_id,
        label=leg_.label,
        start=_pt(leg_.start),
        end=_pt(leg_.end),
        opens=_pt(leg_.opens),
        deadline=_pt(leg_.deadline),
        holiday_slate=sorted(survivor.HOLIDAY_SLATES.get(leg_.leg_id, frozenset())),
        pick=_pick_out(pick) if pick else None,
    )


def _pick_out(pick: survivor.SurvivorPick) -> PickOut:
    leg_ = survivor.leg(pick.leg_id)
    game = _leg_games(leg_).get(pick.game_id)
    deadline = (
        survivor.pick_deadline_for(leg_, game.start_time) if game else None
    )
    return PickOut(
        leg_id=pick.leg_id,
        team=pick.team,
        game_id=pick.game_id,
        locked_by=pick.locked_by,
        locked_at=_pt(pick.locked_at),
        etsn=pick.etsn,
        result=pick.result,
        effective_deadline=_pt(deadline) if deadline else None,
    )


class SurvivorGameOut(BaseModel):
    game_id: str
    away_team: str
    home_team: str
    start_time: str  # Pacific ISO
    early_kickoff: bool  # kicks before the leg deadline: this pick due at kickoff
    consensus: float | None  # market home spread (median across books)
    predicted_line: float | None  # power-rating model home spread
    home_win_prob: float | None  # market: devigged ML consensus, else spread-implied
    away_win_prob: float | None
    model_win_prob: float | None  # D-035 two-lens blend (home side)
    ml_lens_prob: float | None  # moneyline-implied strengths lens
    spread_lens_prob: float | None  # spread-ratings lens
    home_used: str | None  # leg the team was burned in, if any
    away_used: str | None
    divisional: bool


class SurvivorBoardOut(BaseModel):
    leg_id: str
    label: str
    opens: str
    deadline: str
    seconds_to_deadline: int
    locked: bool  # past the leg deadline
    captain: str
    pick_locked: bool
    holiday_slate: list[str]
    games: list[SurvivorGameOut]


@router.get("/board", response_model=SurvivorBoardOut)
def get_board(leg: str | None = None) -> SurvivorBoardOut:
    """The leg's slate with the survivor-relevant market read: consensus
    spread, model line, and implied straight-up win probability, plus which
    teams the entry has already burned."""
    now = contest_api._now()
    if leg is None:
        current = survivor.leg_for(now)
        if current is None:
            raise HTTPException(
                status_code=400,
                detail="The season is over; pass ?leg= explicitly.",
            )
        leg_ = current
    else:
        leg_ = _leg_or_422(leg)

    odds = contest_api._open_odds()
    store = _store()
    try:
        games = sorted(
            odds.games(window=(leg_.start, leg_.end)),
            key=lambda g: (g.start_time, g.game_id),
        )
        fitted = contest.power_ratings(odds)
        fitted_ml = valuation.implied_strengths(odds)
        histories = {g.game_id: contest.spread_history(odds, g.game_id) for g in games}
        ml_pairs = {
            g.game_id: valuation.book_probs(valuation.moneyline_history(odds, g.game_id))
            for g in games
        }
        used = store.used_teams()
        pick = store.pick(leg_.leg_id)
    finally:
        store.close()
        odds.close()
    ratings, hfa = fitted if fitted else ({}, 0.0)
    ml_strengths, ml_hfa = fitted_ml if fitted_ml else ({}, None)

    rows = []
    for g in games:
        market = contest.consensus(contest.book_spreads(histories[g.game_id]))
        model_line = contest.predicted_home_spread(ratings, hfa, g.home_team, g.away_team)
        # Market straight-up probability: devigged moneyline consensus when
        # books quote it, else the spread-implied conversion (D-035).
        ml_consensus = valuation.consensus_prob(ml_pairs[g.game_id])
        reference = market if market is not None else model_line
        home_wp = (
            ml_consensus
            if ml_consensus is not None
            else survivor.win_probability(reference) if reference is not None else None
        )
        ml_lens = (
            valuation.model_home_prob(ml_strengths, ml_hfa, g.home_team, g.away_team)
            if ml_hfa is not None
            else None
        )
        model_wp, spread_lens = model.nfl_model_prob(ml_lens, model_line)
        rows.append(
            SurvivorGameOut(
                game_id=g.game_id,
                away_team=g.away_team,
                home_team=g.home_team,
                start_time=_pt(g.start_time),
                early_kickoff=g.start_time < leg_.deadline,
                consensus=market,
                predicted_line=model_line,
                home_win_prob=home_wp,
                away_win_prob=round(1 - home_wp, 3) if home_wp is not None else None,
                model_win_prob=model_wp,
                ml_lens_prob=ml_lens,
                spread_lens_prob=spread_lens,
                home_used=used.get(g.home_team),
                away_used=used.get(g.away_team),
                divisional=(
                    NFL_DIVISIONS.get(g.home_team) is not None
                    and NFL_DIVISIONS.get(g.home_team) == NFL_DIVISIONS.get(g.away_team)
                ),
            )
        )
    return SurvivorBoardOut(
        leg_id=leg_.leg_id,
        label=leg_.label,
        opens=_pt(leg_.opens),
        deadline=_pt(leg_.deadline),
        seconds_to_deadline=int((leg_.deadline - now).total_seconds()),
        locked=now >= leg_.deadline,
        captain=contest.captain_for(
            survivor.LEG_INDEX[leg_.leg_id] + 1, contest_api._members()
        ),
        pick_locked=pick is not None,
        holiday_slate=sorted(survivor.HOLIDAY_SLATES.get(leg_.leg_id, frozenset())),
        games=rows,
    )


class RankedChoiceIn(BaseModel):
    team: str
    note: str = ""


class SurvivorProposalIn(BaseModel):
    leg: str
    member: str
    # Preference order: first = A choice, then B, C (D-033).
    choices: list[RankedChoiceIn] = Field(min_length=1, max_length=3)


class SurvivorProposalOut(BaseModel):
    member: str
    team: str
    note: str
    rank: int  # 1 = A, 2 = B, 3 = C


class SurvivorProposalsOut(BaseModel):
    leg_id: str
    submitted: list[str]
    waiting_on: list[str]
    proposals: list[SurvivorProposalOut]  # own always; everyone's once submitted


@router.post("/proposal", response_model=SurvivorProposalsOut, status_code=201)
def submit_proposal(body: SurvivorProposalIn, request: Request) -> SurvivorProposalsOut:
    """A member's blind team for the leg: one shot, immutable — same honesty
    rule as the Million proposals."""
    contest_api._require_member(body.member)
    contest_api._enforce_identity(request, body.member)
    leg_ = _leg_or_422(body.leg)
    for choice in body.choices:
        _game_for_team(leg_, _require_team(choice.team))
    store = _store()
    try:
        used = store.used_teams()
        burned = [c.team for c in body.choices if c.team in used]
        if burned:
            raise HTTPException(
                status_code=409,
                detail=f"already used: {', '.join(f'{t} (leg {used[t]})' for t in burned)}",
            )
        try:
            store.submit_proposal(
                leg_.leg_id,
                body.member,
                [(c.team, c.note) for c in body.choices],
                submitted_at=contest_api._now(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _proposals_view(store, leg_.leg_id, body.member)
    finally:
        store.close()


@router.get("/proposals", response_model=SurvivorProposalsOut)
def get_proposals(leg: str, member: str, request: Request) -> SurvivorProposalsOut:
    """Own proposal always; the whole group's only after yours is submitted."""
    contest_api._require_member(member)
    contest_api._enforce_identity(request, member)
    leg_ = _leg_or_422(leg)
    store = _store()
    try:
        return _proposals_view(store, leg_.leg_id, member)
    finally:
        store.close()


def _proposals_view(
    store: survivor.SurvivorStore, leg_id: str, member: str
) -> SurvivorProposalsOut:
    submitted = store.submitted_members(leg_id)
    mine_only = member not in submitted
    rows = store.proposals(leg_id, member=member if mine_only else None)
    return SurvivorProposalsOut(
        leg_id=leg_id,
        submitted=submitted,
        waiting_on=[m for m in contest_api._members() if m not in submitted],
        proposals=[
            SurvivorProposalOut(member=p.member, team=p.team, note=p.note, rank=p.rank)
            for p in rows
        ],
    )


class SurvivorVoteIn(BaseModel):
    leg: str
    member: str
    team: str


class TeamCandidateOut(BaseModel):
    team: str
    backers: list[str]  # top-choice backers (status ladder)
    status: str
    points: int  # A=3, B=2, C=1 across members (D-033)
    support: list[list[object]]  # [member, rank] pairs; rank 0 = vote


class PickWarningOut(BaseModel):
    severity: str  # info | warning | fatal
    message: str


class SurvivorConsensusOut(BaseModel):
    leg_id: str
    captain: str
    candidates: list[TeamCandidateOut]
    working_pick: TeamCandidateOut | None  # top candidate — what would lock now
    working_pick_warnings: list[PickWarningOut]
    effective_deadline: str | None  # min(leg deadline, working pick's kickoff)
    pick_locked: bool


@router.post("/vote", response_model=SurvivorConsensusOut)
def cast_vote(body: SurvivorVoteIn, request: Request) -> SurvivorConsensusOut:
    """Set (or change) your stance to one team. Requires your blind proposal
    in; closed once the pick is locked."""
    contest_api._require_member(body.member)
    contest_api._enforce_identity(request, body.member)
    leg_ = _leg_or_422(body.leg)
    _game_for_team(leg_, _require_team(body.team))
    store = _store()
    try:
        _require_submitted(store, leg_.leg_id, body.member)
        if store.pick(leg_.leg_id) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"leg {leg_.leg_id} pick is locked; voting is over",
            )
        used = store.used_teams()
        if body.team in used:
            raise HTTPException(
                status_code=409,
                detail=f"{body.team} was already used in leg {used[body.team]}",
            )
        store.cast_vote(
            leg_.leg_id, body.member, body.team, cast_at=contest_api._now()
        )
        return _consensus_view(store, leg_)
    finally:
        store.close()


@router.get("/consensus", response_model=SurvivorConsensusOut)
def get_consensus(leg: str, member: str, request: Request) -> SurvivorConsensusOut:
    """The reveal: everyone's stances tallied. Blind rule applies."""
    contest_api._require_member(member)
    contest_api._enforce_identity(request, member)
    leg_ = _leg_or_422(leg)
    store = _store()
    try:
        _require_submitted(store, leg_.leg_id, member)
        return _consensus_view(store, leg_)
    finally:
        store.close()


def _require_submitted(store: survivor.SurvivorStore, leg_id: str, member: str) -> None:
    if not store.has_submitted(leg_id, member):
        raise HTTPException(
            status_code=409,
            detail=f"{member} has not submitted a leg-{leg_id} proposal yet — "
            "propose first, then the reveal unlocks.",
        )


def _consensus_view(
    store: survivor.SurvivorStore, leg_: survivor.Leg
) -> SurvivorConsensusOut:
    candidates = survivor.tally_teams(
        store.proposals(leg_.leg_id), store.votes(leg_.leg_id), contest_api._members()
    )
    working = candidates[0] if candidates else None
    warnings: list[PickWarningOut] = []
    deadline = None
    if working is not None:
        used = store.used_teams()
        picks = store.all_picks()
        warnings = [
            PickWarningOut(severity=w.severity, message=w.message)
            for w in survivor.pick_warnings(working.team, leg_.leg_id, used, picks)
        ]
        games = _leg_games(leg_)
        ours = [
            g for g in games.values() if working.team in (g.home_team, g.away_team)
        ]
        if ours:
            deadline = survivor.pick_deadline_for(
                leg_, min(g.start_time for g in ours)
            )
    return SurvivorConsensusOut(
        leg_id=leg_.leg_id,
        captain=contest.captain_for(
            survivor.LEG_INDEX[leg_.leg_id] + 1, contest_api._members()
        ),
        candidates=[
            TeamCandidateOut(
            team=c.team, backers=list(c.backers), status=c.status,
            points=c.points, support=[[m, r] for m, r in c.support],
        )
            for c in candidates
        ],
        working_pick=(
            TeamCandidateOut(
                team=working.team, backers=list(working.backers),
                status=working.status, points=working.points,
                support=[[m, r] for m, r in working.support],
            )
            if working
            else None
        ),
        working_pick_warnings=warnings,
        effective_deadline=_pt(deadline) if deadline else None,
        pick_locked=store.pick(leg_.leg_id) is not None,
    )


class SurvivorPickIn(BaseModel):
    leg: str
    member: str
    team: str


class LockedPickOut(BaseModel):
    pick: PickOut
    warnings: list[PickWarningOut]  # future-constraint notes, recorded at lock


@router.post("/pick", response_model=LockedPickOut, status_code=201)
def lock_pick(body: SurvivorPickIn, request: Request) -> LockedPickOut:
    """Lock the leg's selection of record. Hard rules enforced here: team not
    already used (Rule 15a — a repeat is a disqualification), pick before the
    effective deadline (leg deadline or the team's kickoff, whichever is
    first), one pick per leg with no changes (Rule 18). Future-constraint
    warnings (holiday slates) are returned, not enforced — a bad idea is
    still a legal pick."""
    contest_api._require_member(body.member)
    contest_api._enforce_identity(request, body.member)
    leg_ = _leg_or_422(body.leg)
    game = _game_for_team(leg_, _require_team(body.team))
    now = contest_api._now()
    deadline = survivor.pick_deadline_for(leg_, game.start_time)
    if now >= deadline:
        raise HTTPException(
            status_code=409,
            detail=(
                f"past this pick's effective deadline ({_pt(deadline)}) — the leg"
                " deadline, or kickoff if the team's game starts earlier."
            ),
        )
    store = _store()
    try:
        warnings = survivor.pick_warnings(
            body.team, leg_.leg_id, store.used_teams(), store.all_picks()
        )
        try:
            store.lock_pick(
                leg_.leg_id,
                body.team,
                game.game_id,
                locked_by=body.member,
                locked_at=now,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        pick = store.pick(leg_.leg_id)
    finally:
        store.close()
    assert pick is not None
    logger.info(
        "survivor leg %s locked by %s: %s (%s)",
        leg_.leg_id,
        body.member,
        body.team,
        game.game_id,
    )
    return LockedPickOut(
        pick=_pick_out(pick),
        warnings=[PickWarningOut(severity=w.severity, message=w.message) for w in warnings],
    )


class SurvivorEtsnIn(BaseModel):
    leg: str
    etsn: str = Field(min_length=1, max_length=40)


@router.patch("/pick", response_model=PickOut)
def record_etsn(body: SurvivorEtsnIn) -> PickOut:
    """Attach Circa's confirmation (the 12-digit ETSN) — proof the pick made
    it into the contest."""
    leg_ = _leg_or_422(body.leg)
    store = _store()
    try:
        try:
            store.set_etsn(leg_.leg_id, body.etsn)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        pick = store.pick(leg_.leg_id)
    finally:
        store.close()
    assert pick is not None
    return _pick_out(pick)


@router.get("/pick", response_model=PickOut)
def get_pick(leg: str) -> PickOut:
    leg_ = _leg_or_422(leg)
    store = _store()
    try:
        pick = store.pick(leg_.leg_id)
    finally:
        store.close()
    if pick is None:
        raise HTTPException(status_code=404, detail=f"no locked pick for leg {leg}")
    return _pick_out(pick)


class SurvivorResultIn(BaseModel):
    leg: str
    result: str = Field(pattern="^(win|loss)$")


@router.post("/result", response_model=PickOut)
def record_result(body: SurvivorResultIn) -> PickOut:
    """Enter (or correct) the straight-up result. Enter ties as a loss — that
    is how the contest grades them (Rule 6a)."""
    leg_ = _leg_or_422(body.leg)
    store = _store()
    try:
        try:
            store.record_result(leg_.leg_id, body.result)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        pick = store.pick(leg_.leg_id)
    finally:
        store.close()
    assert pick is not None
    return _pick_out(pick)


class SurvivorAutoGradeOut(BaseModel):
    leg_id: str
    result: str | None  # written this call; None if skipped
    skipped_reason: str | None
    pick: PickOut


@router.post("/result/auto", response_model=SurvivorAutoGradeOut)
def auto_grade(leg: str) -> SurvivorAutoGradeOut:
    """Grade the leg's pick from the ESPN final score (free, unmetered).
    Ties grade as losses (Rule 6a). Safe to re-run — a corrected final
    overwrites, and a non-final game is skipped with a reason."""
    leg_ = _leg_or_422(leg)
    store = _store()
    try:
        pick = store.pick(leg_.leg_id)
        if pick is None:
            raise HTTPException(
                status_code=404, detail=f"no locked pick for leg {leg}"
            )
        game = _leg_games(leg_).get(pick.game_id)
        if game is None:
            return SurvivorAutoGradeOut(
                leg_id=leg_.leg_id,
                result=None,
                skipped_reason="game not in odds database",
                pick=_pick_out(pick),
            )
        eastern = ZoneInfo("America/New_York")  # ESPN groups scoreboard days in ET
        source = contest_api._finals_source()
        try:
            finals = {
                (f.away_team, f.home_team): f
                for f in source.fetch_final_scores(
                    game.start_time.astimezone(eastern).date()
                )
            }
        except ProviderError as exc:
            raise HTTPException(
                status_code=502, detail=f"finals source failed: {exc}"
            ) from exc
        final = finals.get((game.away_team, game.home_team))
        if final is None:
            return SurvivorAutoGradeOut(
                leg_id=leg_.leg_id,
                result=None,
                skipped_reason="no final score found",
                pick=_pick_out(pick),
            )
        if not final.completed:
            return SurvivorAutoGradeOut(
                leg_id=leg_.leg_id,
                result=None,
                skipped_reason="game not final",
                pick=_pick_out(pick),
            )
        result = survivor.grade_survivor_pick(
            pick, game, final.home_score, final.away_score
        )
        store.record_result(leg_.leg_id, result)
        logger.info("survivor leg %s auto-graded: %s -> %s", leg_.leg_id, pick.team, result)
        pick = store.pick(leg_.leg_id)
    finally:
        store.close()
    assert pick is not None
    return SurvivorAutoGradeOut(
        leg_id=leg_.leg_id, result=result, skipped_reason=None, pick=_pick_out(pick)
    )


def _require_team(team: str) -> str:
    if team not in NFL_CODES:
        raise HTTPException(status_code=422, detail=f"unknown NFL team code {team!r}")
    return team
