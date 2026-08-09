# FF Agent — Feasibility & Hypothesis Analysis

*Research date: 2026-08-09. Companion to `claude.md`.*

## Context

`claude.md` (consolidated from a claude.ai planning session) proposes a multi-league fantasy
football decision-support system built on a specific thesis: **don't out-predict the platforms,
predict their bias.** The plan is to measure per-platform positional mispricing from historical
ADP vs. actual finish, then use it to adjust a draft board.

The questions were: (a) is the project possible, (b) does the hypothesis hold, and (c) if not, what
should replace it. Rather than reason about it abstractly, I built and ran the proposed analysis on
11 seasons of real data (2015–2025).

**Headline: the project is very feasible, but the hypothesis as specified is not testable with the
proposed data, and the proposed validation metric is broken — it returns "signal" on pure noise.**
A corrected version of the analysis does find one real, well-known effect (the RB dead zone), but
that edge has decayed to roughly zero by 2025. Recommended pivot is in §5, and it preserves the
spirit of the original idea while making it actually actionable.

---

## 1. Corrections to `claude.md` (verified)

| Claim in doc | Reality |
|---|---|
| "for the 2025 season" | It's **2026**. Validation years are 2015–2025; live board is 2026. |
| actuals via `fantasydatapros/data/master/yearly/{year}.csv` | **Dead — 404.** Repo layout changed. |
| nflverse for actuals | Works, but the `player_stats` release tag is **frozen at 2024**. Use the `stats_player` tag: `.../download/stats_player/stats_player_reg_{year}.csv` (2015–2025, 143 cols, has `fantasy_points_ppr`). |
| FFC ADP API | **Confirmed working**, 2015–2026, ~200 players/yr. 2026 already has 5,187 drafts logged (Aug 1–8). |
| "per-platform ADP is harder" | Stronger than that: **historical** per-platform ADP essentially does not exist publicly. But **current-year** per-platform ADP does (FantasyPros, BeatADP, Sleeper). This distinction reshapes the whole project — see §5B. |

Environment note: the local Python 3.14 install has **no SSL root certificates**, so
`pd.read_csv(<https url>)` fails with `CERTIFICATE_VERIFY_FAILED`. Fix once with
`/Applications/Python\ 3.14/Install\ Certificates.command` (or `pip install certifi` + use
`requests`). I worked around it by fetching with `curl`.

---

## 2. Is the project possible? Yes — with one hard constraint

Verified the three platforms' API situations:

| Platform | Auth | Read | **Write (set lineup / waivers / trades)** |
|---|---|---|---|
| **Sleeper** | none needed | Full public API — leagues, rosters, matchups, transactions, drafts | **None. API is read-only by design.** |
| **Yahoo** | OAuth2 **+ approved application** | **Gated — see below** | Yes, if granted (we don't need it) |
| **ESPN** | `espn_s2` + `SWID` cookies | Good, unofficial/undocumented | Undocumented, fragile, ToS-gray |

> **Correction (2026-08-09, verified empirically).** An earlier version of this table said Yahoo
> needed only OAuth2. That is no longer true. Yahoo has moved Fantasy API access behind a
> **manual application review** at `https://sports.yahoo.com/developer/access/`.
>
> Tested end-to-end: the Fantasy Sports permission checkbox has been **removed from self-serve app
> registration**; requesting `scope=fspt-r` on a self-serve app returns `invalid_scope`; and a
> valid OAuth token from such an app returns
> `401 oauth_problem="additional_authorization_required"` on every fantasy endpoint. OAuth itself
> works fine (refresh succeeds, access token issues) — the app simply has no fantasy entitlement.
>
> There is no configuration workaround. Access requires an approved application, with **no
> published turnaround time**.

**The constraint:** you cannot build "an agent that runs all 5 leagues." Sleeper has no write API at
all, and ESPN writes are unofficial. Every platform can be *read*, so a unified **recommendation
engine** across 5 leagues is completely feasible — but execution stays human-in-the-loop. That's
also the right call on ToS grounds. Design for "tell me what to do in all 5 leagues by Tuesday
10am," not "do it for me."

ESPN also tightened access around Aug 2025 (historical `leagueHistory` now requires the cookies).

---

## 3. The hypothesis: tested and falsified as specified

Pulled FFC PPR ADP (2015–2025) and nflverse season actuals, normalized names, and joined:
**1,900 player-seasons at a 98.1% match rate.** Misses are overwhelmingly genuine non-playing
seasons (Bell's holdout, Luck, McKinnon's ACL), not join failures.

### 3a. Running the metric exactly as `claude.md` specifies

`drift = actual_positional_rank − adp_positional_rank`, bucketed by position × round tier:

```
position     QB    RB    TE    WR
1 rds1-2   11.3  14.0   5.5  16.2
2 rds3-5    7.0  14.5   9.4  18.7
3 rds6-10   5.1   8.4  10.6  19.8
4 rds11+    0.2   7.1   9.6  17.5
```

Every bucket is positive. And on the doc's stated "test that matters" — year-by-year sign
consistency — **11 of 16 buckets hit 100% consistency (11/11 seasons)**, with most others at 82–91%.

That looks like overwhelming confirmation. It is actually the tell that the metric is broken.
**A test that everything passes has no discriminating power.**

### 3b. Why it's broken — three independent proofs

**Cause 1 — pool-size mismatch.** The ADP list has only drafted players; actual positional rank is
computed against *every* player at that position:

```
QB:  24 drafted/yr  vs  53-player ranking pool
RB:  61 drafted/yr  vs 141-player pool
WR:  69 drafted/yr  vs 223-player pool
TE:  19 drafted/yr  vs  84-player pool
```

A WR drafted WR30 gets ranked against ~223 WRs. Drift is positive *by construction*, and the
position-to-position differences above mostly just track pool size (WR biggest pool → biggest
"bias"). This alone explains the ordering WR > RB > TE > QB.

**Cause 2 — regression to the mean.** Re-ranking actuals within the drafted cohort only (so both
ranks are permutations of the same set) forces mean drift to exactly 0.0 per position. What's left
is a perfect monotonic gradient:

```
position    QB    RB   TE    WR
1 rds1-2   9.0   9.5  3.8  11.5
2 rds3-5   5.6   7.2  4.1   8.2
3 rds6-10  1.2  -2.0  2.0  -0.4
4 rds11+  -4.6 -11.1 -4.2 -12.9
```

Early picks "underperform," late picks "overperform," in every position. That is the textbook
definition of regression to the mean, not platform bias.

**Cause 3 — Monte Carlo null.** I simulated 300 seasons where ADP is *unbiased for every position*
(true score = −ADP + identical Gaussian noise, no positional effect whatsoever). The metric still
produces positive round 1–2 drift in **100% of simulations** for all four positions — a fake world
with zero bias sails through the sign-consistency test with a perfect 11/11.

**Conclusion: the validation plan in `claude.md` would have "confirmed" the thesis regardless of
whether it's true.** Built as written, it would have produced a board tuned to a statistical
artifact — backed by an impressive-looking 11/11 validation table.

### 3c. The second, subtler trap

The natural fix — compare *points* over expectation at equal draft cost — fails too. It reports QBs
as massively underpriced (+84 to +98 points) at every tier. That's just because QBs score more raw
points than anyone. **You must convert to value over replacement before comparing positions.** This
is exactly why VBD exists, and it means the bias layer can't be built *before* the VBD engine — it
depends on it. That inverts the build order implied in the doc.

---

## 4. What actually survives

Redone properly — VORP (replacement QB14/RB30/WR38/TE13) minus a per-season expectation curve fit
on log(ADP) across all positions:

```
position     QB    RB    TE   WR
1 rds1-2  -26.8  -0.5  28.5  8.3
2 rds3-5   10.4 -21.4  22.9  2.7
3 rds6-10  13.2 -14.3  25.1 -0.9
4 rds11+    1.6 -17.1  42.8 -3.4
```

**One robust finding: the RB dead zone.** RBs drafted rounds 3+ return ~14–21 fewer VORP points than
their draft cost implies. Negative in **11 of 11 seasons**, and the sign holds under shallow and deep
replacement-level assumptions. This is real, and it matches the published Zero RB / dead-zone
literature.

**But it has decayed, and that kills it as a 2026 edge:**

```
2015-2018  mean VOE  -16.4
2019-2022  mean VOE  -23.5
2023-2025  mean VOE   -8.3      2025 alone: -0.1
```

RBs drafted in rounds 3–10 fell from ~35/yr to **26 in 2025** — the market faded them and the
mispricing closed. Corroborated independently: 2026 industry coverage reports RB ADP snapping back
to 2022 levels, ~54% of the first 24 picks being RBs, and "is Zero RB dead" as the season's
consensus debate. `claude.md` predicted exactly this failure mode ("markets partly self-correct");
the data confirms it happened to the single biggest effect.

**Out-of-sample, the whole framework is weak.** Fitting corrections on 2015–2022 and testing on
2023–2025 gives a correlation of only **0.131** between predicted and actual bucket-level VOE.
Positive, but far too weak to hang a draft strategy on.

The apparent TE edge is *not* trustworthy yet — it swings from +13.9 to +48.1 depending purely on
the TE replacement level chosen, which means it's substantially an artifact of pooling all positions
into one expectation curve. Needs position-specific modeling before believing it.

**Net verdict on the thesis:** the *instinct* is sound and the framing is genuinely smart, but
(1) the metric as designed measures noise, (2) blended FFC ADP cannot test *platform* bias at all
since it has no platform split, (3) historical per-platform ADP doesn't exist to fix that, and
(4) done correctly, the surviving positional signal is decaying and weak out-of-sample. **Don't
build the season around it.**

---

## 5. Recommended framework for 2026

Three layers, ordered by confidence and by fit to the actual situation (5 leagues, mixed platforms,
~1 month to first draft). Layer B rescues the original idea in a form that works.

### A. Custom VBD tuned to each league's exact settings — the reliable core

Highest-confidence edge available, and it requires no bias model at all. Opponents draft off generic
platform rankings built for *default* scoring. These leagues aren't default. A VBD board computed
from each league's actual scoring coefficients and actual starter counts (which set the true
replacement level) will systematically disagree with ADP — and those disagreements are structural,
not noise.

Payoff scales with how non-standard a league is. Superflex/2QB is the extreme case (QB values move
enormously), then TE premium, 6-pt passing TDs, 0.5 vs 1.0 PPR, deep benches, IDP. Across 5 leagues
there's almost certainly at least one materially non-standard format, and that's free money relative
to opponents using one generic cheat sheet.

Same machinery as §4 — pointed at a target where the edge is structural rather than predictive.

### B. Live cross-platform ADP arbitrage — the original thesis, salvaged

You don't need *history* to exploit platform bias. You need **today's spread**. Per-platform ADP for
2026 is published right now by FantasyPros (ESPN/Yahoo/CBS/NFL/Sleeper) and BeatADP (Sleeper, ESPN,
Yahoo, Underdog, FantasyPros).

If a player goes at pick 25 on Sleeper and pick 40 on ESPN, then in the ESPN league you can wait —
measured directly, no model, no five-year backtest, no regression artifact. Reframing from *"learn
each platform's bias from history"* (impossible — the data doesn't exist) to *"measure the current
cross-platform spread"* (trivial — it's published) keeps what was good about the thesis and drops
the part that can't be done. It's also strictly better: it captures this year's actual mispricing
rather than assuming last year's persists.

Deliverable per league: a sorted list of "players this platform's field will let you get late" and
"players this platform's field will make you overpay for."

### C. In-season process — where the season is actually won

The draft is one day; waivers run 17 weeks. This is where 5 leagues genuinely overwhelm a human and
where tooling earns the most: five wires, five FAAB budgets, five sets of bye weeks. Ground it in
usage data rather than box scores — nflverse carries snap counts, routes, and targets, so you can
flag role changes (a back's snap share jumping, a receiver's route participation climbing) before
consensus forms on the wire.

**What to drop for now:** the historical bias model. Keep it as a post-season research project using
the corrected VORP-over-expectation methodology in §4. Interesting question; not a 2026 deliverable.

---

## 6. Build plan

Revised order — VBD first, since §3c showed the bias layer depends on it rather than the reverse.

```
data/                       # cached pulls (gitignore raw)
  adp/ffc_{year}_{fmt}.csv
  adp/platform_{year}.csv   # per-platform current-year ADP  (layer B)
  actuals/season_{year}.csv
src/
  names.py                  # ONE shared normalizer — reuse everywhere (98.1% match achieved)
  ingest_adp.py             # FFC + per-platform scrape
  ingest_actuals.py         # nflverse `stats_player` tag (NOT `player_stats`)
  scoring.py                # league scoring dict -> player points
  vbd.py                    # replacement levels from actual starter counts -> VORP
  board.py                  # VORP board + cross-platform ADP delta -> per-league draft board
leagues/
  league_{name}.yaml        # platform, scoring, roster slots, team count, draft date
```

1. **Fix SSL certs** (`Install Certificates.command`), `pip install pandas requests pyyaml`.
2. **`names.py` + ingest.** The normalizer used here (strip accents/punctuation/suffixes, lowercase,
   collapse whitespace) hit 98.1% — reuse it; it's the join's biggest footgun.
3. **`scoring.py` + `vbd.py`.** Replacement level must come from each league's real starter
   requirements — §4's sensitivity table shows conclusions swing hard on this input.
4. **One `league_*.yaml` per league**, then `board.py` → a board per league.
5. **Layer B:** add the per-platform ADP delta column to each board.
6. **After drafts:** in-season waiver/lineup tooling on the same value engine.

### Verification

- Re-run the §3 pipeline as a regression test; assert name-match rate stays >97%.
- **Sanity-check the VBD engine against a known case:** run a superflex config and confirm QBs jump
  dramatically vs. the 1QB board. If they don't, replacement levels are wired wrong.
- Backtest boards: build a 2024 board from 2024 preseason ADP, score it against 2024 actuals, and
  confirm it beats straight-ADP drafting on total VORP.
- Keep the Monte Carlo null harness — **any future "bias" claim must beat a no-bias simulation
  before it goes in the board.** That's the guardrail that would have caught this.

---

## 7. Decisions (settled 2026-08-09)

`claude.md`'s Q2 is answered: **per-platform historical ADP is not obtainable, so blended FFC is the
only option for backtests — but current-year per-platform ADP is free and available, which is what
layer B needs.**

Decisions taken:

| Question | Decision |
|---|---|
| **Direction** | Pivot to **A + B** (custom VBD + live cross-platform ADP), plus the in-season agent. Historical bias model shelved as post-season research. |
| **Write access** | **Read-only, by choice.** No automated roster moves on any platform. Removes the Sleeper write limitation as a concern entirely and keeps everything on the right side of ToS. |
| **Draft agent** | **Live during the draft.** Tracks picks in real time; reports roster gaps, best available, best fit, best value. |
| **In-season cadence** | **Two reports.** Tuesday night = waiver claims + FAAB bids (before Wed AM processing). Wednesday night = post-waiver recap, lineup calls, trade angles. |
| **Delivery** | **Published web page** per report. |

### Layer B source — better than planned

ESPN's `leaguedefaults/3?view=kona_player_info` endpoint returns ESPN's own player ranks in
**STANDARD / PPR / SUPERFLEX** plus auction values, **with no cookie required**. That's the
platform's own opinion straight from the source — no FantasyPros scraping needed. Combined with
Sleeper ADP and FFC blended ADP, that's three independent platform views for the arbitrage layer.

### Live-draft feasibility, by platform

| Platform | Live pick sync | Confidence |
|---|---|---|
| Sleeper | `GET /v1/draft/{draft_id}/picks`, poll every few seconds, no auth | **Verified — easy** |
| ESPN | `?view=mDraftDetail` polling | Unverified under live fast-clock conditions |
| Yahoo | `league/{key}/draftresults` polling | Unverified; possible lag |

A draft is a one-shot event with no retry, so **a manual click-off fallback is required regardless**
of how the polling tests go. Never be blocked by a lagging poller mid-draft.

### In-season data sources (verified working)

- **Sleeper `trending/add`** — raw add counts across Sleeper's entire userbase, unauthenticated.
  A real-time leading indicator of who's about to get bid up.
- **ESPN + Sleeper weekly projections** — blended, for the multi-platform projection view.
- **nflverse** — snap share, route participation, target share. Catches role changes before they
  surface in box scores.
- **News/injury feeds** — for depth-chart context.

### Still needed

League IDs for all 5 leagues — see `leagues/leagues.yaml`. Scoring and roster settings are
auto-derived from each platform's API, so only IDs, draft datetimes, and any house rules are needed
by hand.

---

## Sources

**Data endpoints verified:**
- FantasyFootballCalculator ADP API — `https://fantasyfootballcalculator.com/api/v1/adp/ppr?teams=12&year=YYYY&position=all`
- [nflverse-data releases](https://github.com/nflverse/nflverse-data/releases) — `stats_player` tag

**Platform APIs:**
- [Sleeper API docs](https://docs.sleeper.com/) — read-only, no write endpoints
- [Yahoo Fantasy Sports API](https://yahoofantasysportsapidocs.readthedocs.io/guide/GettingStarted/)
- [cwendt94/espn-api](https://github.com/cwendt94/espn-api) and [mkreiser/ESPN-Fantasy-Football-API](https://github.com/mkreiser/ESPN-Fantasy-Football-API)

**Per-platform ADP (layer B):**
- [FantasyPros ADP](https://www.fantasypros.com/nfl/adp/overall.php) and their [platform ADP variance piece](https://www.fantasypros.com/2024/07/fantasy-football-draft-strategy-adp-variance-on-espn-cbs-nfl-yahoo-sleeper/)
- [BeatADP platform comparison](https://www.beatadp.com/platform-adp)

**Market context for the RB decay finding:**
- [FTN — Is Zero RB Still Viable in 2026?](https://ftnfantasy.com/nfl/is-zero-rb-still-viable-in-2026)
- [RotoWire — Zero RB in 2026](https://www.rotowire.com/football/article/best-ball-strategy-can-zero-rb-work-on-draftkings-in-2026-116677)
- [Sharp Football — 2026 RB ADP historical trends](https://www.sharpfootballanalysis.com/fantasy/running-back-adp/)
- [Adam Harstad, Footballguys — ADP predictiveness](https://www.footballguys.com/article/HarstadRegression04?article=HarstadRegression04)
