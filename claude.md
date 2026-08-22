# Fantasy Football Agent (FF Agent)

## What this project is

A personal fantasy football decision-support system for the **2026 season**, used across
**five leagues** on Sleeper, ESPN and Yahoo, each with different scoring and roster
settings. It reads those leagues, computes a draft board tuned to each one's exact rules,
and produces weekly lineup / waiver / trade recommendations.

**Read-only by design.** It never sets a lineup, submits a claim, or writes to any
platform. Every output is a recommendation Carter acts on himself. Keep it that way:
Sleeper's API is read-only anyway, and ESPN's write endpoints are undocumented and
ToS-grey.

## Read this before changing scoring

Two documents carry the reasoning and are worth more than this file:

- **`ANALYSIS.md`** — the research. Most importantly it records a hypothesis that was
  **tested and rejected**: modelling each platform's positional "bias" from historical ADP
  vs. actual finish. The proposed metric returned a perfect, sign-consistent signal, and a
  Monte Carlo null showed it produced that same signal on data containing no bias at all.
  It was measuring regression to the mean and a pool-size mismatch. **Do not rebuild it.**
- **`README.md`** — what the tool does, from the user's side.

The surviving architecture is conventional **value-based drafting** off blended
projections, plus roster-aware live draft logic. That is the reliable core.

## Ground truth: run the bakeoff

`scripts/bakeoff.py` is the only real validation this system has. Each strategy drafts
from every slot, three seeds, scored as weekly starting-lineup points with byes applied.

    python3 scripts/bakeoff.py [league ...]

Current standing (weekly starting-lineup points vs. following ADP):

    league            board    caps_adp    vorp
    719              +90.8       +41.8    +31.5
    freinds-keeper   +73.6       +24.7   -258.8
    family          +123.6       +68.5    -63.4
    friends         +113.8       +76.3   -143.0
    work            +121.1       +64.6   -181.0

**Run it after any scoring change.** Two things it already taught us: roughly half the
board's edge is just respecting roster-construction limits, and ranking by raw VORP —
which this repo shipped with until it was replaced — is *worse than blindly following ADP*
in four of five leagues.

**Its caveat is not a formality:** rosters are scored with the same projections the board
optimises, so it measures roster *construction*, not whether the projections are any good.
Beating ADP there is not evidence of beating your league.

## Layout

    src/ff/
      names.py         one name normalizer for every join. 98.1% match vs nflverse.
      stats.py         canonical stat vocabulary + each platform's dialect
      leagues.py       pull real league settings into one shape
      projections.py   blended per-stat projections (ESPN + Sleeper + FFToday)
      adp.py           market prices, per-platform where available
      vbd.py           replacement levels from simulated starter demand; rosterable depth
      board.py         static board (vorp, edge) + live_ranks() for remaining supply
      draft.py         live draft state, marginal lineup value, roster_max, recommend()
      starts.py        expected starts: byes, injury rates, snap share
      why.py           one or two sentences on why a pick is the pick
      lineup.py        weekly projections, availability, lineup optimiser
      weekly.py        usage trends, free agents, waiver targets
      trades.py        trade search and evaluation
      keepers.py       keeper valuation
      notify.py        email delivery
    scripts/
      draft_server.py  DRAFT DAY. localhost:8777, all leagues behind a tab strip.
      bakeoff.py       strategy validation (see above)
      injury_rates.py  measures MISS_RATE from nflverse
      build_boards.py  writes boards/*.md
      explain.py       trace one player through every step of the ranking
      weekly_report.py / send_weekly.py / keepers.py / draft.py / yahoo_auth.py
    leagues/leagues.yaml   gitignored — league IDs and manual settings
    data/raw/              gitignored — cached pulls

## How the board ranks, and where it stops working

Rounds 1-6, ranking is `marginal lineup value + expected value of your next pick`. Both
are real numbers driven by lineup maths and ADP survival. Trust it here.

**From about round 7 the lineup maths flatlines.** Once your starters are full, a bench
player adds exactly 0 starting-lineup points *by definition*, so `marginal` and `plan` are
0.0 for the entire board. This is structural, not a bug, and no replacement level fixes it
(dynamic replacement was tried; it doesn't). Late ordering is instead carried by:

- `draft.roster_max` — you start one TE, so a third is never the pick. This is the backstop
  that stops the board recommending nine tight ends, which it provably did.
- `starts.bench_value` — expected starts x value over a **waiver streamer**, floored at
  zero. The floor matters: without it, covering more weeks makes a bad player rank *worse*,
  so the board preferred backups who shared your bye week.

Late-round ordering leans on `starts.MISS_RATE`, measured over 2018-2025 by
`scripts/injury_rates.py`. Treat late rounds as a shortlist, not an instruction.

## Gotchas that have already cost time

- **Team codes disagree.** Projection feeds say `LAR`/`JAC`, nflverse says `LA`/`JAX`.
  Unmapped, 29 players silently looked as though they never had a bye. Use
  `starts.TEAM_ALIASES` / `starts.bye_for()`, never a bare dict lookup.
- **This Python has no SSL roots.** `urllib` fails with CERTIFICATE_VERIFY_FAILED; use
  `requests` (it carries certifi). Everything in `src/ff` already does.
- **`draft_server.py` embeds its whole UI in one Python string.** Backslashes are consumed
  by Python before the browser sees them — `\s` and `̀` both need doubling. A JS
  string interpolated into an `onclick` attribute broke every name with an apostrophe.
- **Names are the join's biggest footgun.** One normalizer, `ff.names`. Do not write a second.
- **Yahoo needs manual API approval**, not just OAuth. Self-serve apps get
  `invalid_scope` / `additional_authorization_required`.
- **FFToday 403s intermittently.** A failing projection source is skipped, not fatal.

## Working agreements

- **Verify, don't assert.** Every claim about behaviour in this repo should come from
  running something. Several "improvements" this codebase has shipped were later shown by
  bakeoff to be actively harmful.
- **State assumptions as assumptions.** `MISS_RATE` sat as invented constants until it was
  measured, and every one was far too low. If a number is a guess, say so at its
  definition.
- **Say what didn't work.** Commit messages here record failed approaches (dynamic
  replacement not fixing the flatline, the two biased ways of sampling injury rates)
  because that is what stops them being retried.

## Current state

Five leagues configured; all drafts for 2026 are done. The `work` league's pick file is
partially recorded (manual entry during a live Yahoo draft) and needs reconciling once the
Yahoo API key lands.

Next up, in Carter's priority order: **week-to-week in-season logic** (lineups, waivers,
trades on the shared value engine). A guillotine/elimination league variant was scoped and
deliberately dropped — that draft has passed.
