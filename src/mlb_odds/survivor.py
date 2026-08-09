"""Circa Survivor consensus tool: the 20-leg calendar, one-pick workflow, and
the constraint math that keeps a single entry alive.

The contest (see circa-survivor-2026-rules.md) is one straight-up winner per
"Contest Week", each team usable once, loss/tie/missed-deadline = elimination.
2026 has up to twenty legs: NFL Weeks 1-18 plus two standalone holiday legs —
Thanksgiving Eve/Day/Black Friday (Rule 8) and the Christmas Leg (Rule 9) —
each with its own selection window, deadline, and restricted team slate.

What makes survivor different from the Million tool (contest.py):
- One team per leg, not five picks: proposals/votes/locks carry a team code.
- Constraints compound across the season: every lock burns a team for all
  remaining legs, and the holiday legs are elimination traps for entries that
  arrive with their whole slate already used. Warning math for that lives
  here, next to the calendar that defines it.
- A tie grades as a LOSS (Rule 6a), and a missed deadline eliminates the
  entry (Rule 12) — there is no Million-style "score 0 and continue".

Scheduling is Pacific wall-clock (the rules' timezone), leg windows are
half-open [start, end), and the odd boundaries around the holiday legs are
transcribed from Rule 11 rather than derived.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from mlb_odds.models import Game
from mlb_odds.teams import NFL_CODES

PACIFIC = ZoneInfo("America/Los_Angeles")

# Rule 8a: the teams scheduled across Thanksgiving Eve (Wed Nov 25), Day
# (Thu Nov 26), and Black Friday (Fri Nov 27). Rule 9a: the Christmas Leg
# slate (Thu Dec 24 / Fri Dec 25). Used for *planning* warnings only — pick
# validation trusts the stored schedule, which wins if the NFL moves a game.
THANKSGIVING_TEAMS = frozenset(
    {"GB", "LAR", "CHI", "DET", "PHI", "DAL", "KC", "BUF", "DEN", "PIT"}
)
CHRISTMAS_TEAMS = frozenset({"HOU", "PHI", "GB", "CHI", "BUF", "DEN", "LAR", "SEA"})

# NFL margins have a fat-tailed but roughly normal spread around the market
# line; sigma ~13.45 points is the standard fit. Good enough for ranking
# "how safe is this favorite", which is all survivor needs.
_MARGIN_SIGMA = 13.45


def win_probability(home_spread: float) -> float:
    """P(home wins outright) implied by a home spread (negative = favored).

    Ties are folded into the loss side, matching Rule 6a's grading — this is
    the survivor-relevant probability, not the three-way market."""
    z = -home_spread / (_MARGIN_SIGMA * math.sqrt(2))
    return round(0.5 * (1 + math.erf(z)), 3)


@dataclass(frozen=True)
class Leg:
    """One survivor selection: an NFL week or a standalone holiday leg.

    `start`/`end` bound the games that belong to the leg (half-open);
    `opens`/`deadline` bound when its pick may be submitted at Circa. A pick
    of a team kicking off before the deadline is due at kickoff instead —
    that per-team tightening is `pick_deadline_for`, not a Leg field.
    """

    leg_id: str  # "1".."18", "TG", "XMAS"
    label: str
    start: datetime
    end: datetime
    opens: datetime
    deadline: datetime


def _pt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=PACIFIC)


def _build_legs() -> tuple[Leg, ...]:
    """The 2026-27 calendar, transcribed from Rules 7/11/12/13.

    Normal weeks run Wed 2:00 AM -> Wed 2:00 AM with a Wed 10:00 AM window
    open and a Sat 4:00 PM deadline. The holiday legs and the two week
    fragments that follow them get the explicit boundaries Rule 11 spells
    out; weeks 11 and 15 are truncated where the holiday weeks begin early.
    """
    from mlb_odds.contest import week_window

    def normal(week: int, *, end: datetime | None = None) -> Leg:
        w_start, w_end = week_window(week)
        return Leg(
            leg_id=str(week),
            label=f"Week {week}",
            start=w_start,
            end=end or w_end,
            opens=datetime.combine(w_start.date(), time(10, 0), tzinfo=PACIFIC),
            deadline=_saturday_deadline(w_start),
        )

    legs: list[Leg] = [normal(w) for w in range(1, 11)]
    # Week 11 ends where the Thanksgiving Contest Week begins (Tue Nov 24 2 AM).
    legs.append(normal(11, end=_pt(2026, 11, 24, 2)))
    # Thanksgiving Eve/Day + Black Friday: its own Contest Week (Rules 8/11/12a).
    legs.append(
        Leg(
            leg_id="TG",
            label="Thanksgiving",
            start=_pt(2026, 11, 24, 2),
            end=_pt(2026, 11, 28, 0),
            opens=_pt(2026, 11, 24, 10),
            deadline=_pt(2026, 11, 25, 16),
        )
    )
    # Week 12 fragment: Sat Nov 28 12:00 AM -> Tue Dec 1 1:59 AM (Rule 11),
    # window opens at the fragment start (Rule 12), deadline that Saturday.
    legs.append(
        Leg(
            leg_id="12",
            label="Week 12",
            start=_pt(2026, 11, 28, 0),
            end=_pt(2026, 12, 1, 2),
            opens=_pt(2026, 11, 28, 0),
            deadline=_pt(2026, 11, 28, 16),
        )
    )
    legs.extend(normal(w) for w in (13, 14))
    # Week 15 ends where the Christmas Contest Week begins (Tue Dec 22 2 AM).
    legs.append(normal(15, end=_pt(2026, 12, 22, 2)))
    # Christmas Leg: Thu Dec 24 / Fri Dec 25 games (Rules 9/11/13). No early
    # Tuesday window in the rules for this one — it opens the normal Wednesday.
    legs.append(
        Leg(
            leg_id="XMAS",
            label="Christmas",
            start=_pt(2026, 12, 22, 2),
            end=_pt(2026, 12, 26, 0),
            opens=_pt(2026, 12, 23, 10),
            deadline=_pt(2026, 12, 24, 16),
        )
    )
    # Week 16 fragment: Sat Dec 26 12:00 AM -> Tue Dec 29 1:59 AM.
    legs.append(
        Leg(
            leg_id="16",
            label="Week 16",
            start=_pt(2026, 12, 26, 0),
            end=_pt(2026, 12, 29, 2),
            opens=_pt(2026, 12, 26, 0),
            deadline=_pt(2026, 12, 26, 16),
        )
    )
    legs.extend(normal(w) for w in (17, 18))
    return tuple(legs)


def _saturday_deadline(week_start: datetime) -> datetime:
    return datetime.combine(
        week_start.date() + timedelta(days=3), time(16, 0), tzinfo=PACIFIC
    )


LEGS: tuple[Leg, ...] = _build_legs()
LEG_INDEX: dict[str, int] = {leg.leg_id: i for i, leg in enumerate(LEGS)}
HOLIDAY_SLATES: dict[str, frozenset[str]] = {
    "TG": THANKSGIVING_TEAMS,
    "XMAS": CHRISTMAS_TEAMS,
}


def leg(leg_id: str) -> Leg:
    try:
        return LEGS[LEG_INDEX[leg_id]]
    except KeyError:
        raise ValueError(f"unknown survivor leg {leg_id!r}") from None


def leg_for(at: datetime) -> Leg | None:
    """The leg to be working on at instant `at`: the one containing it, or the
    next upcoming one (pre-season -> leg 1). None once the season is over."""
    if at.tzinfo is None:
        raise ValueError("leg_for requires an aware datetime")
    for candidate in LEGS:
        if at < candidate.end:
            return candidate
    return None


def pick_deadline_for(leg_: Leg, kickoff: datetime) -> datetime:
    """When THIS team's pick is due: the leg deadline, or kickoff if earlier
    (you cannot pick a team whose game has started). Unlike the Million's
    Rule 8, an early game never moves anyone else's deadline — survivor is
    one pick, so only the picked team's kickoff matters."""
    return min(leg_.deadline, kickoff)


# --- store -------------------------------------------------------------------

SURVIVOR_MIGRATIONS: list[str] = [
    # Same one-entry-per-thing discipline as the Million tables: proposals are
    # blind and immutable (one team per member per leg), votes are a member's
    # latest stance, picks are the locked selection of record (Rule 18: no
    # voids or changes) with the straight-up result entered after grading.
    """
    CREATE TABLE survivor_proposals (
        leg          TEXT NOT NULL,
        member       TEXT NOT NULL,
        team         TEXT NOT NULL,
        note         TEXT NOT NULL DEFAULT '',
        submitted_at TEXT NOT NULL,
        PRIMARY KEY (leg, member)
    );

    CREATE TABLE survivor_votes (
        leg     TEXT NOT NULL,
        member  TEXT NOT NULL,
        team    TEXT NOT NULL,
        cast_at TEXT NOT NULL,
        PRIMARY KEY (leg, member)
    );

    CREATE TABLE survivor_picks (
        leg       TEXT PRIMARY KEY,
        team      TEXT NOT NULL UNIQUE,
        game_id   TEXT NOT NULL,
        locked_by TEXT NOT NULL,
        locked_at TEXT NOT NULL,
        etsn      TEXT,
        result    TEXT CHECK (result IN ('win', 'loss'))
    );
    """,
]


@dataclass(frozen=True)
class SurvivorProposal:
    leg_id: str
    member: str
    team: str
    note: str


@dataclass(frozen=True)
class SurvivorVote:
    leg_id: str
    member: str
    team: str


@dataclass(frozen=True)
class SurvivorPick:
    leg_id: str
    team: str
    game_id: str
    locked_by: str
    locked_at: datetime
    etsn: str | None
    result: str | None  # win | loss; a tie is recorded as loss (Rule 6a)


class SurvivorStore:
    """Survivor app state. Lives in the same SQLite file as the Million tables
    (one contest-state file to deploy and back up) but under its own version
    table, so the two migration lists never interleave (D-028)."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS survivor_schema_version (version INTEGER NOT NULL)"
        )
        row = self._conn.execute("SELECT version FROM survivor_schema_version").fetchone()
        current = row[0] if row else 0
        for version, script in enumerate(
            SURVIVOR_MIGRATIONS[current:], start=current + 1
        ):
            self._conn.executescript(script)
            self._conn.execute("DELETE FROM survivor_schema_version")
            self._conn.execute(
                "INSERT INTO survivor_schema_version (version) VALUES (?)", (version,)
            )
            self._conn.commit()

    # -- blind proposals ------------------------------------------------------

    def submit_proposal(
        self, leg_id: str, member: str, team: str, note: str, *, submitted_at: datetime
    ) -> None:
        """One blind team per member per leg, immutable once in — same honesty
        rule as the Million: no editing after the reveal."""
        _require_leg(leg_id)
        _require_team(team)
        if self.has_submitted(leg_id, member):
            raise ValueError(f"{member} already submitted a proposal for leg {leg_id}")
        with self._conn:
            self._conn.execute(
                "INSERT INTO survivor_proposals (leg, member, team, note, submitted_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (leg_id, member, team, note, _to_utc_iso(submitted_at)),
            )

    def has_submitted(self, leg_id: str, member: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM survivor_proposals WHERE leg = ? AND member = ?",
                (leg_id, member),
            ).fetchone()
            is not None
        )

    def submitted_members(self, leg_id: str) -> list[str]:
        return [
            row[0]
            for row in self._conn.execute(
                "SELECT member FROM survivor_proposals WHERE leg = ?"
                " ORDER BY submitted_at",
                (leg_id,),
            )
        ]

    def proposals(
        self, leg_id: str, *, member: str | None = None
    ) -> list[SurvivorProposal]:
        sql = "SELECT leg, member, team, note FROM survivor_proposals WHERE leg = ?"
        params: tuple[object, ...] = (leg_id,)
        if member is not None:
            sql += " AND member = ?"
            params = (leg_id, member)
        return [
            SurvivorProposal(leg_id=lg, member=m, team=t, note=n)
            for lg, m, t, n in self._conn.execute(sql + " ORDER BY member", params)
        ]

    # -- stances --------------------------------------------------------------

    def cast_vote(
        self, leg_id: str, member: str, team: str, *, cast_at: datetime
    ) -> None:
        """A member's latest stance for the leg (one team); re-voting replaces."""
        _require_leg(leg_id)
        _require_team(team)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO survivor_votes (leg, member, team, cast_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (leg, member) DO UPDATE
                    SET team = excluded.team, cast_at = excluded.cast_at
                """,
                (leg_id, member, team, _to_utc_iso(cast_at)),
            )

    def votes(self, leg_id: str) -> list[SurvivorVote]:
        return [
            SurvivorVote(leg_id=lg, member=m, team=t)
            for lg, m, t in self._conn.execute(
                "SELECT leg, member, team FROM survivor_votes WHERE leg = ?"
                " ORDER BY member",
                (leg_id,),
            )
        ]

    # -- the locked pick ------------------------------------------------------

    def lock_pick(
        self,
        leg_id: str,
        team: str,
        game_id: str,
        *,
        locked_by: str,
        locked_at: datetime,
    ) -> None:
        """The leg's selection of record: one pick per leg, one use per team
        for the whole contest. The UNIQUE(team) constraint is the last line of
        the Rule 15a defence — the software must never record a repeat."""
        _require_leg(leg_id)
        _require_team(team)
        if self.pick(leg_id) is not None:
            raise ValueError(f"leg {leg_id} pick is already locked")
        used = self.used_teams()
        if team in used:
            raise ValueError(
                f"{team} was already used in leg {used[team]} — Rule 15a makes a"
                " repeat pick a disqualification"
            )
        with self._conn:
            self._conn.execute(
                "INSERT INTO survivor_picks (leg, team, game_id, locked_by, locked_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (leg_id, team, game_id, locked_by, _to_utc_iso(locked_at)),
            )

    def pick(self, leg_id: str) -> SurvivorPick | None:
        row = self._conn.execute(
            "SELECT team, game_id, locked_by, locked_at, etsn, result"
            " FROM survivor_picks WHERE leg = ?",
            (leg_id,),
        ).fetchone()
        if row is None:
            return None
        return SurvivorPick(
            leg_id=leg_id,
            team=row[0],
            game_id=row[1],
            locked_by=row[2],
            locked_at=datetime.fromisoformat(row[3]),
            etsn=row[4],
            result=row[5],
        )

    def all_picks(self) -> dict[str, SurvivorPick]:
        legs_with_picks = [
            row[0] for row in self._conn.execute("SELECT leg FROM survivor_picks")
        ]
        return {lg: p for lg in legs_with_picks if (p := self.pick(lg)) is not None}

    def used_teams(self) -> dict[str, str]:
        """team -> leg it was burned in, from locked picks."""
        return {
            team: lg
            for lg, team in self._conn.execute("SELECT leg, team FROM survivor_picks")
        }

    def set_etsn(self, leg_id: str, etsn: str) -> None:
        with self._conn:
            updated = self._conn.execute(
                "UPDATE survivor_picks SET etsn = ? WHERE leg = ?", (etsn, leg_id)
            ).rowcount
        if updated == 0:
            raise ValueError(f"no locked pick for leg {leg_id}")

    def record_result(self, leg_id: str, result: str) -> None:
        """Enter (or correct) the straight-up result. Ties are entered as
        'loss' — Rule 6a grades them that way."""
        if result not in ("win", "loss"):
            raise ValueError(f"result must be win/loss, got {result!r}")
        with self._conn:
            updated = self._conn.execute(
                "UPDATE survivor_picks SET result = ? WHERE leg = ?", (result, leg_id)
            ).rowcount
        if updated == 0:
            raise ValueError(f"no locked pick for leg {leg_id}")

    def close(self) -> None:
        self._conn.close()


def _require_leg(leg_id: str) -> None:
    if leg_id not in LEG_INDEX:
        raise ValueError(f"unknown survivor leg {leg_id!r}")


def _require_team(team: str) -> None:
    if team not in NFL_CODES:
        raise ValueError(f"unknown NFL team code {team!r}")


def _to_utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


# --- consensus ---------------------------------------------------------------


@dataclass(frozen=True)
class TeamCandidate:
    """One proposed team with everyone's current stance. Same status ladder as
    the Million: unanimous auto-clears, majority is presumptive, contested
    falls to the week's captain."""

    team: str
    backers: tuple[str, ...]
    status: str  # unanimous | majority | contested


def tally_teams(
    proposals: Sequence[SurvivorProposal],
    votes: Sequence[SurvivorVote],
    members: Sequence[str],
) -> list[TeamCandidate]:
    """Fold proposals + votes into per-team candidates; a member's vote
    overrides their proposal (one stance each). Ordered by backing, so the
    head of the list is the working pick."""
    stance: dict[str, str] = {}
    for p in proposals:
        stance[p.member] = p.team
    for v in votes:
        stance[v.member] = v.team

    backers: dict[str, list[str]] = {}
    for member, team in stance.items():
        backers.setdefault(team, []).append(member)

    candidates = []
    for team, who in backers.items():
        who_ordered = tuple(m for m in members if m in who)
        if len(who_ordered) == len(members):
            status = "unanimous"
        elif len(who_ordered) * 2 > len(members):
            status = "majority"
        else:
            status = "contested"
        candidates.append(TeamCandidate(team=team, backers=who_ordered, status=status))
    candidates.sort(key=lambda c: (-len(c.backers), c.team))
    return candidates


# --- constraint math: what keeps the entry alive -----------------------------


@dataclass(frozen=True)
class HolidayOutlook:
    """How exposed the entry is to one holiday leg's restricted slate."""

    leg_id: str
    label: str
    picked: bool  # that leg's pick is already locked
    remaining: tuple[str, ...]  # slate teams not yet burned (empty = trapped)

    @property
    def danger(self) -> str:
        """none (picked/plenty) | caution (<=4 left) | critical (<=2) | fatal (0)."""
        if self.picked:
            return "none"
        n = len(self.remaining)
        if n == 0:
            return "fatal"
        if n <= 2:
            return "critical"
        if n <= 4:
            return "caution"
        return "none"


def holiday_outlook(
    used: dict[str, str], picks: dict[str, SurvivorPick]
) -> list[HolidayOutlook]:
    return [
        HolidayOutlook(
            leg_id=leg_id,
            label=leg(leg_id).label,
            picked=leg_id in picks,
            remaining=tuple(sorted(slate - set(used))),
        )
        for leg_id, slate in HOLIDAY_SLATES.items()
    ]


@dataclass(frozen=True)
class PickWarning:
    severity: str  # info | warning | fatal
    message: str


def pick_warnings(
    team: str, leg_id: str, used: dict[str, str], picks: dict[str, SurvivorPick]
) -> list[PickWarning]:
    """What locking `team` for `leg_id` does to the entry's future options.

    Holiday-slate warnings fire only for legs still ahead of this one whose
    pick isn't locked: burning a Thanksgiving team IN the Thanksgiving leg is
    the point, not a problem. Fatal = the lock guarantees elimination later
    (Rules 8/9: no eligible team left when the leg arrives)."""
    warnings: list[PickWarning] = []
    for holiday_id, slate in HOLIDAY_SLATES.items():
        if LEG_INDEX[leg_id] >= LEG_INDEX[holiday_id] or holiday_id in picks:
            continue
        if team not in slate:
            continue
        label = leg(holiday_id).label
        remaining_after = sorted(slate - set(used) - {team})
        if not remaining_after:
            warnings.append(
                PickWarning(
                    severity="fatal",
                    message=(
                        f"{team} is the last unused {label} team — locking it"
                        f" guarantees elimination at the {label} leg (no eligible"
                        " pick will remain)"
                    ),
                )
            )
        else:
            severity = "warning" if len(remaining_after) <= 2 else "info"
            warnings.append(
                PickWarning(
                    severity=severity,
                    message=(
                        f"{team} is {label}-eligible: {len(remaining_after)} of"
                        f" {len(slate)} slate teams would remain"
                        f" ({', '.join(remaining_after)})"
                    ),
                )
            )
    return warnings


@dataclass(frozen=True)
class EntryStatus:
    alive: bool
    survived: int  # legs with a graded win
    reason: str | None = None  # why eliminated
    at_leg: str | None = None


def entry_status(picks: dict[str, SurvivorPick], *, now: datetime) -> EntryStatus:
    """Walk the legs in order and report the first fatal event, if any.

    Elimination causes, per the rules: a graded loss (ties grade as losses,
    Rule 6a) or a leg whose deadline passed with no pick locked (Rule 12).
    An ungraded pick is not fatal — it is just pending."""
    survived = sum(1 for p in picks.values() if p.result == "win")
    for leg_ in LEGS:
        p = picks.get(leg_.leg_id)
        if p is None:
            if now >= leg_.deadline:
                return EntryStatus(
                    alive=False,
                    survived=survived,
                    reason="no pick locked by the deadline",
                    at_leg=leg_.leg_id,
                )
            continue
        if p.result == "loss":
            return EntryStatus(
                alive=False,
                survived=survived,
                reason=f"{p.team} lost (ties grade as losses)",
                at_leg=leg_.leg_id,
            )
    return EntryStatus(alive=True, survived=survived)


def grade_survivor_pick(pick: SurvivorPick, game: Game, home_score: int, away_score: int) -> str:
    """Straight-up grading: win only if the picked team scored more points.
    A tie is a loss (Rule 6a). Forfeits (NFL-awarded W/L with no score) are
    the one case this can't see — record those by hand."""
    if pick.team == game.home_team:
        ours, theirs = home_score, away_score
    elif pick.team == game.away_team:
        ours, theirs = away_score, home_score
    else:
        raise ValueError(f"{pick.team} did not play in {game.game_id}")
    return "win" if ours > theirs else "loss"
