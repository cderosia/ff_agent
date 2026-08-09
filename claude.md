# Fantasy Football Agent (FF Agent)

## What this project is

A personal fantasy football decision-support system for the 2025 season, used across
**multiple leagues** (ESPN / Yahoo / Sleeper, different scoring + roster settings). Treat
this season as a **pilot** to test the core thesis and the architecture; expect to throw
things away and rebuild for next year.

Four intended assistants, in priority order:

1. **Draft assistant** (BUILD FIRST — season starts in ~1 month) — produce a bias-adjusted
   draft board per league.
2. **Lineup assistant** — set weekly lineups given a roster.
3. **Trade assistant** — evaluate/craft trades to fill roster holes.
4. **Waiver/free-agency assistant** — monitor waivers and surface pickups.

All four should share one underlying player-value engine, so build that engine once.

## The core thesis (what makes this different)

Do **not** try to out-predict player performance from raw stats — ESPN/Yahoo/Sleeper/DK
already do this with far more data and staff. We will lose that game.

Instead, **predict the bias of the predictors.** The hypothesis: platforms and their
userbases systematically mis-value certain player types (e.g. "ESPN over-inflates RB
preseason expectations, so RBs go too early in ESPN drafts and underperform their draft
slot"). If a bias is **real and repeats year over year**, it's an exploitable edge that
survives even when it's widely known, because it's driven by defaults and human psychology,
not a fixable model error. This is analogous to finding closing-line value in betting: you
don't beat the market by predicting the dice, you beat it by spotting where the line is
mispriced.

### Honest assessment of the thesis (from planning session)

- **It's a better bet than out-projecting the platforms**, because it competes where a solo
  builder can actually win (relative mispricing in *your* leagues) instead of where you
  can't (absolute projection accuracy).
- **But it's higher-variance / more likely to produce a null result.** The bias must be
  large enough to matter and consistent across years, not small-sample noise. Small buckets
  over 3–5 years is the #1 failure mode — be disciplined about sample size and
  out-of-sample testing.
- **Markets partly self-correct.** Big obvious biases get faded by sharp drafters, shrinking
  them. The exploitable biases are likely the subtle structural ones.
- **Recommended framing: layered, not all-or-nothing.** Use conventional **value-based
  drafting (VBD / value over replacement)** off decent projections as the reliable floor,
  and use the bias model as a **tiebreaker / edge-finder** on top. If the bias signal is
  weak, you still have a solid draft assistant. If it's strong, you have an edge no one in
  your leagues has.

## Data-sourcing reality (discovered this session — READ THIS, saves you dead ends)

The hard part is historical data. Key findings:

- **Nobody warehoused historical *preseason projections*** from ESPN/Yahoo/Sleeper. Scrapers
  (e.g. `ffanalytics`) only work in real time. So "learn each platform's projection bias"
  needs a **proxy**.
- **The workable proxy is historical ADP (Average Draft Position) by platform.** Where people
  draft on a platform is downstream of that platform's default rankings + userbase bias, so
  **per-platform historical ADP vs. actual end-of-season positional finish** is a solid
  stand-in for "platform X over/undervalues Y."
- **ADP source:** FantasyFootballCalculator public API is the canonical free archive:
  `https://fantasyfootballcalculator.com/api/v1/adp/{ppr|standard|half-ppr|2qb|dynasty}?teams=12&year=YYYY&position=all`
  Returns JSON: `players[]` with `name, position, team, adp, adp_formatted, times_drafted`.
  NOTE: FFC ADP is aggregated across its own userbase, not split by ESPN/Yahoo/Sleeper.
  True per-platform ADP (esp. Sleeper) is harder — investigate Sleeper's API and paid
  sources if per-platform split proves essential. For a v1, FFC's blended ADP is enough to
  test whether *any* positional/tier bias exists.
- **Actuals (season fantasy points / positional finish):** very easy.
  - `nfl_data_py` / `nflreadr` (nflverse) — cleanest, goes back decades.
  - Or fantasydatapros yearly CSVs: `https://raw.githubusercontent.com/fantasydatapros/data/master/yearly/{year}.csv`
    (has FantasyPoints, standard scoring; columns include Player, Tm, Pos, receiving/rushing/passing).
  - `hvpkod/NFL-Data` (`https://raw.githubusercontent.com/hvpkod/NFL-Data/main/NFL-data-Players/{year}/{week}/{POS}.csv`)
    is **weekly** actuals+projections from Fantasy.NFL.com back to 2015 — useful later for
    lineup/waiver work, NOT for preseason draft bias.
- **Why this was built in Claude Code, not the desktop sandbox:** the desktop sandbox has no
  direct network egress (pip/pypi and direct HTTP to these hosts are blocked); only an
  in-context web-fetch tool worked, which can't feed pandas. Claude Code on the local
  machine has real network + package installs, so it's the right tool.

## Recommended architecture (v1)

```
data/                      # raw + cached pulls (gitignored raw, commit small processed CSVs)
  adp/ffc_{year}_{format}.csv
  actuals/season_{year}.csv
src/
  ingest_adp.py            # pull FFC ADP by year/format -> tidy df
  ingest_actuals.py        # pull nflverse/fantasydatapros actuals -> tidy df (season pts + positional finish)
  bias_model.py            # join ADP<->actuals by normalized name+pos+year;
                           #   compute drift = actual_positional_rank - adp_positional_rank;
                           #   bucket by position x draft-tier (rounds 1-2/3-5/6-10/11+);
                           #   aggregate mean drift + year-by-year sign consistency
  vbd.py                   # value-based drafting: replacement levels per league settings
  board.py                 # apply bias correction to this year's live ADP -> adjusted board
leagues/
  league_{name}.yaml       # platform, scoring (ppr/half/std), roster slots, team count
notebooks/                 # exploration
```

Player-name normalization is the join's biggest footgun (Jr./III, D.J. vs DJ, team
abbreviations). Build one shared normalizer and reuse it everywhere.

## Validation plan (do this BEFORE trusting the model)

1. Pull ADP + actuals for **at least 5 seasons** (e.g. 2019–2024).
2. For each position × draft-tier bucket, compute mean drift **per year**.
3. **The test that matters: is the sign of the drift consistent across years?** A bias that
   flips sign year to year is noise. A bias that's the same direction in 4–5 of 5 years, with
   meaningful magnitude (e.g. early-round RBs finish ~1+ tier worse than ADP), is a real edge.
4. Out-of-sample check: fit correction on years 1..n-1, test on year n.
5. Only fold surviving biases into the board; leave the rest to VBD.

## Immediate next steps

1. Scaffold repo (structure above), set up venv, `pip install nfl_data_py pandas requests pyyaml`.
2. Write `ingest_adp.py` (FFC) and `ingest_actuals.py` (nflverse); cache to `data/`.
3. Write `bias_model.py` and **run the validation plan** — this decides whether the whole
   thesis holds. Report per-bucket mean drift + year-by-year sign consistency.
4. If signal is real: build `vbd.py` + `board.py`, add one `league_*.yaml`, generate a draft
   board. If signal is weak: ship the VBD board alone and treat bias as a stretch goal.
5. Later: lineup / trade / waiver assistants on top of the shared value engine.

## Open questions for Carter

I will answer these tomorrow

- Which specific leagues (platform + scoring + team count + roster slots) this season?
- Is per-platform ADP split (ESPN vs Yahoo vs Sleeper) essential to you, or is blended FFC
  ADP acceptable for the pilot? (Blended is much easier; per-platform may need paid data.)
- Draft dates — how much runway before the first draft?
