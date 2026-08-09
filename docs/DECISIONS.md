# Decision log

Format: one entry per decision, newest at the bottom. Revisit by appending a new
entry, never by rewriting history.

## D-001 — Python (2026-07-09)
Standard stack for sports-data/quant work; pandas interop was an explicit output
requirement. TypeScript and Go considered and rejected (no web-app consumer, and
ad-hoc analysis matters more than daemon ergonomics).

## D-002 — Pluggable provider architecture (2026-07-09)
Odds sources are volatile: APIs change pricing, scrapers break, books come and go.
A one-method protocol (`OddsProvider.fetch_game_lines`) isolates that churn. Cost is
one indirection layer; accepted.

## D-003 — The Odds API as the v1 provider (2026-07-09)
Chosen over scraping DraftKings/FanDuel (brittle, ToS-gray, maintenance-heavy) and
aggregator scraping (same issues). Free tier: 500 credits/month; a game-lines call
costs 3 credits (3 markets × 1 region) → ~5 polls/day free. That bounds v1 polling
frequency; per-book depth across ~15 US books is the payoff. ESPN's free consensus
endpoint is the planned no-key fallback provider (roadmap M4).

## D-004 — v1 scope: game lines only (2026-07-09)
Moneyline, run line, totals. Props multiply volume 10–20× and cost more credits;
futures and live odds need different polling models. All deferred, none precluded by
the schema (market is a TEXT column, not an enum in SQL).

## D-005 — SQLite storage (2026-07-09)
Volume math: ~2,430 games/season × ~90 rows/snapshot × ~290 snapshots/game ≈ 60–65M
rows ≈ 5–10 GB/season at aggressive (5-min) polling — comfortably within SQLite
range for years. The real limits are topological, and we accept them knowingly:
- exactly one writer process (WAL mode: many readers + one writer) — fine, there is
  one collector;
- local-file access only — collector and analysis run on the same machine;
- never on NFS.
Migration trigger: a second writer or a second machine → Postgres. Hedge: portable
SQL schema, storage behind its own module. Analytical speed is not a migration
trigger — DuckDB can query the SQLite file directly.

## D-006 — Append-only snapshots, no dedup in v1 (2026-07-09)
Every fetch cycle writes all quotes even if unchanged. Simpler writes, simpler
"what did the board look like at time T" queries. Space is cheap per D-005.
`changed_only` mode is roadmap if the file ever gets annoying.

## D-007 — American odds as canonical price (2026-07-09)
US books quote American; The Odds API returns it natively as integers (no float
round-trip). Decimal is derived on the model (`price_decimal` property), never stored.

## D-008 — Canonical game identity (2026-07-09)
`game_id = "{date_utc}-{away}-{home}-{game_number}"`. Deterministic across providers
(no provider's native ID is privileged), human-readable, doubleheader-safe.
Provider-native IDs kept in a side table for traceability/debugging.

## D-009 — Game identity is reconciled in storage, not per-fetch (2026-07-09)
The Odds API drops finished games from the /odds feed, so a provider can only
number doubleheaders from what the current response contains — game 2 alone in
the feed looks like game 1. `Storage.store` therefore treats provider-assigned
game_numbers as provisional: a native id seen before keeps its stored game_id,
and a new game never takes a game_id already claimed by a different native id
from the same provider (its number is bumped). Consequence: game_number is
stable-first-assignment, not strictly start-time-ordered, when the halves of a
doubleheader first appear in different cycles. Stability wins (FR2 / D-008).

## D-010 — Cross-provider convergence requires start-time agreement (2026-07-09)
D-009's native-id claim check is per-provider by design, so a second provider's
first sighting of a doubleheader converges onto whatever game_id its provisional
number computes — which is wrong when its feed has already dropped game 1 and it
numbers game 2 as 1, misfiling game 2's quotes under game 1's id (and rule 1 then
freezes the bad mapping). Guard: a new native id may only take an *existing*
game_id if the stored start_time is within 2 hours of the incoming one; otherwise
the number is bumped, same as a same-provider claim conflict. 2 hours is below any
doubleheader gap (game 1 alone runs ~2.5h+) but above realistic provider disagreement
about one game's first pitch. Accepted tradeoff: a game rescheduled by >2h within
the same day, first seen by a new provider only after the move while another
provider stored the old time, splits into two game_ids — rarer and cheaper than
cross-wiring two physical games' line histories.

## D-011 — .env support at the CLI layer only (2026-07-11)
The CLI loads a `.env` file from the working directory via `python-dotenv`
(`load_dotenv()` in the Typer app callback), so cron jobs and local runs can keep
`THE_ODDS_API_KEY` / `MLB_ODDS_DB` in one gitignored, chmod-600 file instead of a
shell profile or crontab line. The library never reads `.env`: `import mlb_odds`
must not have cwd-dependent side effects when embedded in a larger app — library
users pass `TheOddsAPI(api_key=...)` or set real env vars. Real environment
variables always win (`load_dotenv` does not override existing vars). New runtime
dep `python-dotenv`: zero transitive dependencies, clears the minimal-deps bar.

## D-012 — The API resolves the database server-side, read-only (2026-07-25)
The HTTP API's first revision copied `_resolve_db` from the CLI, which accepts a
`--db` override. In the CLI that flag is trusted local-operator configuration; as
an HTTP query parameter it became unauthenticated attacker input flowing into
`sqlite3.connect` on a connection that opens read-write and runs `_migrate()`.
Confirmed consequences: arbitrary-path SQLite file creation, reading any other
mlb-odds database the process could reach, and injecting this schema (plus a
permanent `journal_mode=WAL` flip) into any unrelated SQLite database on the host
— a browser cookie store, for instance. Two changes, defence in depth:

1. No endpoint takes a `db` parameter. The API path comes from `MLB_ODDS_DB` or
   the default, resolved server-side. Database location is deployment config.
2. `Storage(..., read_only=True)` opens via the `mode=ro` URI and skips both the
   WAL pragma and `_migrate()`, so an API process cannot create or write a
   database even if a future change reintroduces a caller-supplied path. The path
   is passed through `Path.resolve().as_uri()` so `?`/`#` cannot smuggle extra URI
   parameters. A missing database is a 503, not a silently created empty one.

`OddsClient` additionally rejects `read_only=True` with a non-empty `providers`
list. The API's inability to burn The Odds API credits was previously incidental
(it happened to pass `providers=[]`); this makes it a checked invariant, since a
"refresh now" endpoint is an obvious future addition and 167 unauthenticated
requests would exhaust a month of free-tier quota (3 credits/poll, 500/month).

Consequence: the API cannot apply migrations. A database created by an older
build is migrated on the next CLI or collector run; until then the API reads it
at the old schema. Given the collector must run anyway for the data to exist,
this is not a practical ordering hazard.

## D-013 — Local-day queries use a UTC instant window, not a UTC date (2026-07-25)
`games(on_date)` / `latest_odds(on_date)` match `substr(start_time, 1, 10)`, i.e.
a UTC calendar date. The `today` board is a *local*-day view, so filtering by UTC
date is the wrong question: a 10pm PDT first pitch is 05:00Z the following day and
vanishes from its own board. The CLI sidestepped this by fetching everything and
filtering in Python — correct, but it made the query cost grow with the whole
append-only `odds` table (measured: 1.07s to render ~15 games from a 216k-row
database, and rising for the life of the database) because the inner
`MAX(fetched_at)` aggregate groups over every row ever stored.

Both methods therefore take an optional `window=(start, end)` of timezone-aware
UTC instants, half-open, which the API computes for the viewer's local day. Bounds
are normalized to UTC before comparison, preserving the invariant that makes
lexical ISO-8601 comparison equal instant comparison (see `models._require_utc`).
`on_date` is retained and unchanged for callers that genuinely mean a UTC date.

Migration 2 adds `CREATE INDEX idx_games_start ON games (start_time)` so the range
scan is an index seek rather than a table scan — without it the window helps the
empty-day case only. Measured on the same 216k-row database: 1.07s → 0.006s (185x)
for a day with games, confirmed via `EXPLAIN QUERY PLAN` to use the new index.

## D-014 — Doubleheader convergence must search, not just bump (2026-07-29)
The test-rigor backlog's "cross-provider doubleheader, reverse order" item
exposed a real defect, not a coverage gap: `_resolve_game_id` started from the
provider's own game number and only ever bumped upward. A provider that saw the
doubleheader in the opposite order numbers the halves 2/1; its early half
(numbered 2) could never reach canonical id 1 and split off as a phantom game 3.
Resolution now first tries to converge onto any stored same-slate game whose
start_time is within SAME_GAME_START_TOLERANCE and whose id isn't claimed by a
different native id of the same provider; only then does it fall back to the
bump-until-free allocation for genuinely new games. D-008/D-010 semantics are
unchanged — this widens the candidate set, not the matching rules.

## D-015 — changed_only dedups against the newest row only (2026-07-30)
`--changed-only` skips a quote when its (line, price) equals the newest stored
row for the same (game, provider, book, market, outcome). The baseline is
deliberately the newest row, not "any prior value": a reversion (120 → 125 →
120) must write all three rows or history would show a phantom 125 as current.
The dedup lives in Storage.store, not the collector, so library users get the
same semantics. Trade-off documented in the README: with changed_only, history
records changes rather than polls — the absence of a row at time T means
"unchanged since the previous row", not "book dropped out". latest_odds is
unaffected either way, because it already carries last-known quotes forward.
Default stays append-everything; storage cost was never the motivation for the
default, auditability of "we polled at T and saw X" was.

## D-016 — ESPN provider: unofficial endpoint, book taken from the response (2026-07-30)
The M4 item said "ESPN consensus-line provider", but ESPN no longer surfaces a
consensus: its scoreboard carries one partner sportsbook's lines per event
(DraftKings at recording time, 2026-07-30). The provider therefore takes the
book name from `odds[].provider.displayName` (lowercased, spaces stripped)
instead of hardcoding "consensus" or "espn" — if ESPN changes partners, rows
appear under the new book's name rather than silently mislabeled. Caveats
accepted: the endpoint (site.api.espn.com scoreboard) is unofficial and
undocumented, may change shape without notice, and exposes only the current
line ("close" in ESPN's jargon means "where the line stands now", not a true
closing line). It costs nothing and needs no key, which is what makes it a
useful second provider: it validates cross-provider convergence (D-008) with
real data at zero quota. The shared doubleheader numbering moved to
providers/base.assign_game_numbers so both providers use one implementation.

## D-017 — Live mode gates on the stored slate, not a live-status API (2026-07-30)
`collect --live` polls only while some stored game is inside a fixed window
around its start_time: [first pitch − 15 min, first pitch + 4 h). The
alternative — asking a scores API whether a game is actually in progress —
would add a runtime dependency and another unofficial endpoint for marginal
gain: the fixed window wastes at most a few post-game polls per slate, and 4h
covers the overwhelming majority of games in the pitch-clock era (an
extra-innings marathon just loses its tail). Consequences accepted: the slate
must already be in the database (run a normal collect first — the collector
logs this), and rain delays poll through the delay. Between windows the loop
idles on an interruptible wait and re-checks the slate every 30 min, so a
daily cron collect that lands new games wakes it naturally. Credit math:
--live with the metered provider at the default 300s interval costs ~36
credits per 3h game — pair it with a paid tier, or use ESPN once the
--provider flag lands (both PRs are independent and compose).

## D-018 — Player props: ladders are rungs, boards stay game-only (2026-07-30)
The recorded response settled two design questions. First, props arrive as
*ladders* — one player, several lines (Over 1.5 and Over 2.5 home runs side by
side) — so a prop quote's identity is (book, market, outcome, player, line)
and only price is compared for changed_only; game markets keep their
(book, market, outcome) identity where a line move IS the movement. Second,
board queries (latest_odds, closing_odds, today/API views) exclude prop rows
entirely (player IS NULL filter): a "latest per key" board over ladders is a
different product surface, and pretending rungs are board cells would collapse
them. Props are consumed through history_df/odds_df/export, which now carry a
player column (NULL for game markets; schema migration 3).

Markets are a curated Literal (batter_home_runs, batter_hits,
batter_total_bases, pitcher_strikeouts) rather than a free string: each
addition multiplies credit cost, and typos would silently store garbage.
Credit model differs from game lines: the events list is free, but each
event's odds request costs [markets returned] x [regions] — a full 15-game
slate at two markets can spend ~30 credits, so `mlb-odds props` is one
snapshot per invocation (cron it deliberately, no loop mode) and prints
credits remaining. Fixtures: events list and the betrivers home-run ladder
are a live recording (2026-07-30, 1 credit); the draftkings strikeouts
over/under book is an edited addition for coverage, labeled as such.

## D-019 — Multi-sport via sport-parameterized providers, one database per sport (2026-07-31)
NFL support generalizes in place rather than forking the package. Shape of the
change: providers take `sport="mlb"|"nfl"` (sport key, endpoint, and market
mapping are per-instance), `teams.normalize(sport, provider, name)` namespaces
the 30 MLB + 32 NFL canonical codes per sport, and The Odds API's "spreads" /
ESPN's "pointSpread" map to `run_line` for baseball and `spread` for football —
one semantic market, sport-appropriate names.

The load-bearing decision is **one database per sport** (`--sport nfl` defaults
to ./nfl-odds.sqlite / $NFL_ODDS_DB): canonical game_ids are
date-away-home-number, and MLB KC / NFL KC are different franchises that could
genuinely collide on the same date under one roof. A `sport` column was
rejected — it would touch every query, index, and uniqueness rule for a
dimension that never joins across itself (no MLB@NFL games), whereas separate
files isolate it for free and keep per-sport quota/backup/retention independent.

Deliberately deferred: NFL player props (curated MLB markets don't transfer;
needs its own market curation and fixture recording), and the web API/UI stays
pointed at the MLB database ($MLB_ODDS_DB) until it grows a sport switcher.
The package keeps its historical `mlb_odds` name — a rename to something
sport-neutral is pure churn until an actual third sport forces the question.
Fixtures recorded live 2026-07-31: one 3-credit NFL game-lines poll (484
remaining) and a free ESPN fetch that caught the Hall of Fame preseason game
with full odds.

## D-020 — Circa Million consensus tool: separate app-state DB, home-spread edge convention (2026-08-06)
The contest tool (contest.py + contest_api.py, C1 of the plan in
circa-million-2026-rules.md) is a consumer of the odds database, not part of
the collection pipeline: `contest_api → contest → storage`, odds DB opened
read-only with the same rationale as api.py. Contest state (manually entered
Circa lines; later: proposals, votes, cards) lives in its own SQLite file
($CONTEST_DB, default ./contest.sqlite) rather than a table in the odds DB.
The two files have different write authorities — the odds DB is an append-only
record written by the collector, contest lines are human-entered and
correctable (upsert) — and different backup/retention needs. Same linear
migration-list discipline as storage.py.

Contest lines are manual input by design: Circa's contest spreads are static,
contest-only numbers that exist on no commercial feed. The API rejects lines
for game_ids not already stored inside the target contest week's window, so a
typo cannot create a phantom row that never joins to the board.

Edge math conventions (locked here so every future surface agrees):
- every spread is the HOME line (negative = home favored), as rendered
  everywhere else in the package;
- consensus = median of the newest home spread per book (median so one stale
  book cannot drag the reference); a book absent from newer snapshots keeps
  its last-known number — under --changed-only absence means "unchanged",
  which is what makes as-of movement math correct rather than a fallback;
- edge = contest_line - consensus; positive → value on home, negative → away;
- movement_since_entry = consensus now - consensus as of the line's
  entered_at (re-entering a line re-anchors it);
- key numbers ±3/±7 flag only strict crossings, landing exactly on one is not
  a cross.

The contest calendar (Rule 19 Wednesday-2AM-PT weeks, Saturday 4 PM deadline,
Thursday/holiday-Wednesday line posts) is hardcoded to the 2026 season — the
contest is a single season, and a config surface for dates nobody will change
is speculative. Rule 8 card-level early deadlines are C2 scope with the card
model itself.

## D-021 — Consensus workflow: blind one-shot proposals, stance model, env-config members (2026-08-06)
C2/C3 of the contest tool. The load-bearing choices:

**Blind proposals are one-shot and immutable.** The blind phase exists to
prevent anchoring; it only works if a member cannot peek at the reveal and
then edit. Submission is 1-5 picks in a single POST; the reveal (and voting)
unlocks per-member only after that member's own set is in (HTTP 409 before).

**One stance per (member, game): a vote overrides that member's proposal.**
Proposals and votes are not separate tallies — a member who proposed home and
later votes away has changed their mind, not split themselves. Candidates
group stances by (game, side); unanimous auto-tops the list, majority is
strictly > half, everything else is contested and falls to the week's captain
(rotation = configured member order, week 1 -> first).

**Card locking mirrors Circa's own rules.** Exactly 5 distinct games, one
card per week, no edits after lock (voting closes too). Rule 8 is enforced at
lock time against the *chosen* picks: the effective deadline is the earlier of
Saturday 4 PM PT and the earliest selected kickoff — a card that includes an
already-kicked-off early game is refused. ETSN is recorded post-submission as
proof the card physically made it into the contest.

**Grading is manual entry** (win/loss/push per pick, re-entry corrects): the
odds DB stores prices, not final scores, and 5 results/week for 18 weeks is
not worth a scores provider. The season view derives everything from graded
cards: points, the 14c tiebreaker ladder in rule order, quarter totals, and
booby eligibility (5 picks in every completed week, checked against the week
windows).

**Members come from CONTEST_MEMBERS env, not the database** — three friends
are deployment config, and the ordering doubles as the captain rotation. No
auth beyond that: the tailnet is the security boundary (D-020).

**UI is a single static file** served by contest_api at `/` (registered after
API routes — same shadowing lesson as api.py). No build step, no framework:
one HTML file with fetch calls is serviceable for three users and keeps the
image build free of a second frontend toolchain.

## D-022 — M5 completion: NFL props curation, web sport switcher (2026-08-06)
**NFL prop markets** are curated to four over/under ladders (player_pass_yds,
player_pass_tds, player_rush_yds, player_receptions) — the same shape as the
MLB ladders (name=Over/Under, description=player, point=line), so one parser
serves both sports; non-O/U outcomes (e.g. Anytime TD) are logged skips.
Markets are gated per sport at both the provider and the CLI: requesting an
MLB key against NFL fails before any credit is spent.

**Fixture is edited, not recorded** — and that is documented in the fixture
itself: NFL player props simply are not posted months before game day (the
live probe on 2026-08-06 returned zero bookmakers for the opener). The event
metadata is a real recording from the free events endpoint; the ladders are
hand-authored in the exact shape of the recorded MLB props response. Re-record
a live fixture once the season is close enough for books to hang props.

**Web sport switcher**: every data endpoint takes `?sport=mlb|nfl` (default
mlb). The request picks *which* server-configured database is read — never a
path (D-012 unchanged); each sport keeps its own file per D-019. The board's
middle column keeps its sport-local name in the payload (`run_line` vs
`spread`) and the React UI switches label and key with the toggle.

## D-023 — Auto-grading from ESPN finals (2026-08-06)
Grading a card is pure arithmetic against the static Circa number, so the
split is: `contest.grade_pick()` is pure math (home covers when margin +
home spread > 0; exactly on the number is a push), `ESPN.fetch_final_scores()`
is a provider method (free, unmetered, `?dates=YYYYMMDD` — ESPN groups
scoreboard days in US/Eastern), and contest_api orchestrates: it fetches each
pick-date's slate, matches by canonical team codes, grades picks that have
both a contest line and a completed game, and reports everything else as a
skip with a reason (no line entered / not final / no score found). Re-running
regrades — corrected scores overwrite, in-progress games keep waiting.

This adds the one dependency contest_api didn't have: it now constructs the
ESPN provider (`contest_api → contest → storage` plus `contest_api →
providers.espn`), mirroring how the CLI constructs providers; the contest
domain module stays provider-free. ARCHITECTURE.md updated.

Forfeits are the known gap: the NFL awards a W/L with no final score, and
rules 19a grades the NFL-deemed winner as covering — that case stays manual.
Fixtures: the completed-slate parse test runs against a real recorded MLB
final slate (2026-08-05, trimmed); the NFL week-1 finals fixture is edited
(labeled in-file) because 2026 finals cannot exist yet.

## D-024 — Final scores in the odds database, results collector (2026-08-08)
C4.1 of the contest plan (stats layer). Final scores are collector-written
game facts, so they live in the per-sport odds database (`results` table,
migration 4) rather than the contest file: presence of a row means the game
is final, upsert allows stat corrections, and every future stat (CLV, ATS
records, edge calibration) joins games ⋈ odds ⋈ results in one file.

Provider side: `FinalScore` and a `ScoreSource` protocol move to
providers/base.py (ESPN satisfies it; a future scores source plugs in like an
OddsProvider). `OddsClient.fetch_and_store_results(source, days)` matches
finals to stored games by (away, home) plus start-time proximity using
SAME_GAME_START_TOLERANCE — the same rule as game identity, so MLB
doubleheader halves each get their own score. In-progress games are skipped
and picked up on the next run; a failed day is logged, not fatal.

CLI: `mlb-odds results [--sport nfl] [--date YYYY-MM-DD]`. Default mode
derives the scoreboard days (US/Eastern — how ESPN groups slates) from
stored games started 3+ hours ago with no score, so an empty run costs zero
requests. Free and unmetered; cron freely.

Also fixed operationally alongside (contest doc C4.1): the NFL collection
cron gains a Sunday ~9:55 AM PT poll — without it no true closing line ever
exists for Sunday games, and closing lines are what CLV is measured against.

## D-025 — C4 stats layer: CLV, calibration, market-implied ratings, context (2026-08-08)
Completes the C4 plan (C4.2-C4.5). All four are read-only reports over data
already collected; nothing new is written anywhere.

**One sign convention for edge and CLV** — `pick_side_value(side, contest,
market)`: points of value the *taken side* gets versus a market number,
positive = the contest number is better for that side. With market = current
consensus it is the pick's edge; with market = closing consensus it is CLV.
One function, one convention, no way for the two reports to disagree.

**Calibration** buckets graded picks by at-lock side-adjusted edge (<0,
[0,1), [1,2), >=2) plus a key-number-crossing bucket; the `< 0` bucket is
deliberate — it counts picks made *against* the signal.

**Power ratings** are market-implied: ridge-regularized least squares
(lambda=1, numpy — now a declared direct dependency, previously transitive
via pandas) over one equation per stored game, `r_home - r_away + hfa =
-spread`, using closing consensus for started games and latest otherwise.
Ratings are mean-centered; the board shows the model's predicted line as a
third reference. No play-by-play data, no external feeds — the market rates
the teams, we just solve for what it thinks.

**Context flags** derive purely from the stored schedule (rest days, rest
differential, divisional via a static NFL_DIVISIONS table in teams.py).
**Member stats** grade every member's proposals and final stances against
card results — an opposite-side stance is graded by mirroring the pick's
result (same line, covering is symmetric) — plus per-captain week points.

## D-026 — Public access via Cloudflare Tunnel + Access, header identity (2026-08-08)
Non-tailnet members reach the contest UI through a `cloudflared` container in
the sidecar's network namespace (profile `public`, opt-in): outbound-only
tunnel, still zero published ports, with Cloudflare Access (free tier,
email one-time-PIN allowlist) as the entire login layer — the app implements
no authentication flow.

Identity rides the `Cf-Access-Authenticated-User-Email` header, mapped to a
member via CONTEST_MEMBER_EMAILS. Mapped visitors are locked to their member
(the UI hides the dropdown; acting endpoints 403 on mismatch); an email that
passes Access but isn't mapped is refused on acting endpoints. Tailnet
requests carry no header and keep the original honor system.

The header (not the signed JWT) is deliberately sufficient: the app's only
non-tailnet ingress is the tunnel itself, so nothing untrusted can inject
the header. Documented in the deploy README: if any other public path is
ever added, upgrade to verifying `Cf-Access-Jwt-Assertion`.

## D-027 — Curated bookmakers: Pinnacle in, Bookmaker.eu unavailable (2026-08-09)
The Odds API's `bookmakers` parameter replaces the `regions` selector when
set, and each group of up to 10 named books costs one region-equivalent —
so a curated 10-book list polls at the unchanged 3 credits while adding
books the `us` region lacks. The NFL cron now polls: pinnacle, draftkings,
fanduel, betmgm, williamhill_us, betrivers, betonlineag, lowvig, bovada,
mybookieag. Pinnacle is the point: the sharpest book in the market and the
best single reference for consensus, movement, and CLV (live probe caught
it a full point off the US books on the season opener). The provider caps
the list at 10 (ProviderError) so a poll can't silently double in cost.

Bookmaker.eu was requested and is NOT on The Odds API's feed (probe returned
pinnacle/betonlineag/lowvig/draftkings from
"pinnacle,bookmaker,betonlineag,lowvig,draftkings") — if its lines ever
matter enough, that's a different data source, not a config change.

Consensus stays an unweighted median; pinnacle joins it as one book. A
pinnacle-weighted consensus was considered and rejected for now: the median
already resists one-book outliers, and a weight is a modeling decision to
revisit with CLV data in hand.

## D-028 — Survivor tool: same app, own domain module, own migration chain (2026-08-09)
The Circa Survivor entry manager (see circa-survivor-2026-rules.md) rides the
existing contest server rather than becoming a second app: `survivor_api.py`
is an APIRouter included by `contest_api` at the bottom of its module (so the
router can import contest_api's identity/db/now helpers back as a module
object without a cycle), and the UI is a sibling static page (survivor.html)
next to the Million page. One process, one tunnel, one identity layer.

State lives in the same contest.sqlite file but under survivor's own tables
and its own `survivor_schema_version` table — two independent migration
lists over one file never interleave, and the Million tables stay untouched.

Domain shape differs from the Million deliberately:
- The calendar is 20 *legs* keyed by string ids ("1".."18", "TG", "XMAS"),
  transcribed from Rules 7/11/12/13 rather than derived: the holiday legs and
  the week-12/16 fragments after them have irregular windows, opens, and
  deadlines that don't fit a formula. Weeks 11/15 are truncated where the
  holiday Contest Weeks begin.
- Pick validation trusts the stored NFL schedule (a pick needs a stored game
  in the leg window), while the rules' hardcoded holiday slates
  (THANKSGIVING_TEAMS/CHRISTMAS_TEAMS) power *planning* warnings only — an
  NFL schedule change flows in with the next collect without a code edit.
- Hard rules are enforced (used team = 409 + UNIQUE(team) in SQL as the
  Rule-15a backstop; effective deadline = min(leg deadline, picked team's
  kickoff); one immutable pick per leg); *strategic* mistakes (burning
  holiday-slate teams early) are warnings returned at lock time, not blocks —
  a bad idea is still a legal pick, and the group may have reasons.
- Straight-up win probability shown on the board comes from the market spread
  via a normal margin model (sigma 13.45); ties fold into the loss side to
  match Rule 6a grading. No new dependency — math.erf.

## D-029 — Manual refresh endpoint: the sanctioned exception to D-012 (2026-08-09)
POST /api/refresh pulls current lines on demand (the UI's "pull latest"
button). It is the one endpoint allowed to reach a provider, constrained
three ways so D-012's real goal — HTTP can never spend money or trash the
DB — survives intact:
1. **Free provider only.** It constructs ESPN and nothing else; TheOddsAPI
   is not imported by the endpoint path, and the test suite exercises
   refresh with THE_ODDS_API_KEY unset to prove the metered book can't be
   reached even accidentally.
2. **Debounced** per sport (5 min, HTTP 429) so a stuck finger can't hammer
   ESPN.
3. **Tailnet-only surface.** It lives on the odds-api app, which the public
   tunnel does not route to — the Access-exposed contest app keeps its pure
   no-provider stance.
The write-mode DB open (create/migrate allowed) is acceptable for the same
reason as the CLI's: the path comes from server-side configuration only.

## D-030 — MLB betting dashboard: probability-space valuation (2026-08-09)
The MLB analog of the NFL edge machinery works in probability space because
MLB's core market is the moneyline (run lines are ~always ±1.5; only prices
move). valuation.py: American→prob, multiplicative pair devig (a book's two
prices normalized to sum to 1 — half-quoted pairs are dropped, they devig to
garbage), consensus = median devigged home prob across books with the same
carry-forward semantics as the spread math (D-020), and per-price EV =
p_fair × decimal − 1 against that consensus.

Team strengths are the log-odds twin of D-025's point ratings: one equation
per stored game, logit(p_home) = s_home − s_away + hfa, ridge least squares,
mean-centered; model_edge on the dashboard = model prob − consensus prob.
Recovered exactly (ordering, HFA within 0.06) from a synthetic vigged league
in tests.

GET /api/dashboard?sport&on=YYYY-MM-DD serves any local day (the "today"
window generalized to a date param): per-book ML pairs with devigged probs,
best-EV price per side, consensus/model/edge/drift, and latest run line +
total per book. UI: a Dashboard tab (now the default) with a date navigator,
EV-highlighted best prices, and click-through to the existing LineMovement
charts; market-implied strengths table collapsible below. Works for NFL too
(the math is sport-agnostic) — spread-based tools remain the primary NFL
surface.

## D-031 — Statcast scouting layer: probables + expected stats (2026-08-09)
What Statcast buys a game model, and what we deliberately fetch: probable
starters (the single biggest MLB odds-mover) from the free MLB Stats API,
and Baseball Savant's expected-stats leaderboards (team batting + pitcher
xBA/xSLG/xwOBA/xERA) — expected stats strip batted-ball luck from results,
and the expected-vs-actual gap is what markets price slowly. Deliberately
skipped: pitch-level Statcast (gigabytes, no extra signal at game level)
and defense/OAA (marginal, awkward source).

Plumbing: `mlb-odds statcast [--date]` (all free) stores the day's schedule
via ESPN's scoreboard (`fetch_schedule` — schedule-only games reuse the
same identity reconciliation as odds writes, so a later line poll converges
onto the same game_id), matches statsapi probables by matchup +
SAME_GAME_START_TOLERANCE, and upserts Savant aggregates (migration 5:
probables/statcast_team/statcast_pitcher; Savant's AZ maps to ARI, names
flip from "Last, First"). GET /api/games/{id}/scout joins it all; the UI
renders a matchup card above the movement charts. statsapi full names reuse
the existing MLB name map ("statsapi" provider key in teams.py).

## D-032 — Statcast blended into the MLB model (2026-08-09)
The dashboard's model probability is now a logit-space blend: 70% the
market-implied strength model (D-030), 30% an independent Statcast term.
The Statcast term converts matchup quality to a home-win logit through
literature anchors, not fitted constants: each side's offense = team xwOBA
deviation from the PA-weighted league mean + 0.6 (a starter's typical
innings share) x the opposing probable's xwOBA-against deviation (missing
probable = league-average, i.e. zero); xwOBA gap -> runs via linear weights
(/1.15 x ~38 PA); runs -> logit via the Pythagorean slope (~0.42/run);
market-fitted HFA reused. Degrades gracefully: no probables -> batting-only,
no Statcast -> pure market, no market -> pure Statcast.

The 70/30 weight is an explicit PRIOR, not a fit — documented as such in
the constants. The calibration path ships alongside: a daily free MLB
finals sweep (mlb-results service + cron) accumulates outcomes so the
weight and anchors can be backtested once a real sample exists. UI shows
the blend as the Model column with components on hover.

## D-033 — Mike's process feedback: unlimited stances + pass; Survivor A/B/C (2026-08-09)
Two changes to the consensus workflows, both aimed at the same problem:
maximizing expressed preference so the group aligns faster.

**Million**: proposals lose the 5-pick cap (1-20 stances; a full slate is 16)
and gain an explicit **pass** side — "reviewed, no lean" is information the
captain needs, distinct from silence. Passes back no candidate (excluded from
tallies) but surface in the reveal per game; a vote can withdraw a lean to a
pass. The card is unchanged: exactly five, home/away only. Contest DB
migration 3 rebuilds proposals/votes (SQLite CHECKs are immutable).

**Survivor**: blind proposals become ranked A/B/C (1-3 distinct teams,
preference order; survivor DB migration 2, old single proposals carry over
as A). The status ladder still runs on top choices (vote or A) so
unanimous/majority semantics are unchanged, but candidates now carry Borda
points (A=3, B=2, C=1; a vote replaces a member's whole ranking) and sort by
points — a team that is everyone's B can outrank a team that is one member's
A, which is exactly the alignment case ranked choices exist for. Reveal
shows per-member rank letters and points.

## D-034 — Matchup page: ESPN team-stats lens (2026-08-09)
Clicking a game now lands on a Matchup page (the History tab, renamed):
records + standings and a curated head-to-head season-stat comparison from
ESPN's free site API, with the better side highlighted per row —
direction-aware (ERA/WHIP lower-better; OPS/K9 higher-better), encoded in
MATCHUP_STATS in the ESPN provider so the API and UI can't disagree about
which way is up. Team identity maps by displayName through the existing
name registry (ESPN abbreviations disagree with ours: CHW, AZ...).

Live-fetched, not stored: season aggregates change daily and nothing
downstream models on them (the modeled inputs are Statcast/market — this
lens is for human eyes). In-process TTL caches (1h stats, 24h team ids)
keep a busy page to a few ESPN calls per hour. Works for both sports; the
page keeps the Statcast scout card (MLB) and the LineMovement charts below.

Amended same day: the lens moved to a shared module (matchup.py) so the
contest app serves it too — GET /api/contest/games/{id}/matchup, rendered
by a shared lens.js on both Circa pages: inside the Million board's
expanded chart row, and via click-to-expand on the Survivor board.

## D-035 — Theme: OS preference + manual toggle via data-theme (2026-08-09)
Dark mode was previously scattered `@media (prefers-color-scheme: dark)`
blocks — correct default, but impossible to override from the page. All dark
styles now hang off `[data-theme='dark']` on `<html>`, set before first paint
(no flash) by a tiny theme module: a saved choice in localStorage wins,
otherwise the OS preference applies and live-tracks its changes. The header
gets a ☀️/🌙 toggle that saves the override. A time-of-day scheme (dark
19:00–07:00) was considered and dropped: it fights the OS setting during the
day and surprises users; the OS already encodes the person's actual intent.
