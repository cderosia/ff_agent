# ff_agent

A personal fantasy football decision-support tool. It reads my own leagues across
Sleeper, ESPN, and Yahoo, and generates draft boards and weekly recommendations
tuned to each league's exact scoring and roster settings.

**Read-only by design.** It never makes roster moves, submits waiver claims, or
writes to any platform. Every output is a recommendation I act on myself.

## What it does

- **Draft assistant** — a value-based-drafting (VBD) board computed from each
  league's real scoring coefficients and starter counts, rather than generic
  public rankings. Flags roster gaps, best available, and best value as picks
  come off the board.
- **Weekly reports** — lineup calls, waiver targets with suggested FAAB, and
  trade angles, each with the reasoning shown.

## Why per-league boards

Public rankings and ADP are built for default PPR scoring. Real leagues aren't
default. Across five leagues this tool covers standard scoring, full PPR, 4- and
6-point TDs, combo WR/TE slots, and team counts from 8 to 14 — each of which
moves player values in different directions. A board computed from a league's
actual settings disagrees with generic ADP in ways that are structural, not
guesswork.

## Methodology

[`ANALYSIS.md`](ANALYSIS.md) documents the research behind the approach,
including a hypothesis that was tested and rejected: the original plan was to
model each platform's positional "bias" from historical ADP versus actual
finish. Run against 11 seasons (2015–2025, ~1,900 player-seasons), the proposed
metric returned a strong, perfectly sign-consistent signal — and a Monte Carlo
null showed it produced that same signal on data containing no bias at all. It
was measuring regression to the mean and a pool-size mismatch. The project was
rebuilt on value-over-replacement instead.

## Data sources

- [nflverse](https://github.com/nflverse/nflverse-data) — historical player stats
- [Fantasy Football Calculator](https://fantasyfootballcalculator.com/) — ADP
- Sleeper, ESPN, and Yahoo APIs — league settings and rosters

## Setup

```bash
cp .env.example .env                  # add your own credentials
cp leagues/leagues.example.yaml leagues/leagues.yaml
python3 scripts/yahoo_auth.py         # one-time Yahoo OAuth, if used
```

`.env` and `leagues/leagues.yaml` are gitignored — they hold credentials and
personal league IDs and are never committed.
