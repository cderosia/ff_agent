#!/usr/bin/env python3
"""Which draft slot is best, for a league where you get to choose.

Drafts the league from every slot many times over, playing the board's own
recommendations against a room drafting off jittered ADP, and scores each
resulting roster the way bakeoff.py does: starting-lineup points summed week by
week with byes applied.

    python3 scripts/slot_value.py 719 [--seeds 25]

The number that matters is not the mean, it's the mean against the spread.
Slot effects are small and draft variance is large, so this reports the
standard error and says plainly when the slots are inside the noise -- which
is the usual answer, and a more useful one than a confident fake ranking.

WHAT DECIDES IT: team count, and only team count. Measured by holding roster
shape fixed and swapping team count, in both directions:

    719 settings      8 teams   slot 1 is  -6.5 vs the middle   middle-heavy
    719 settings     10 teams   slot 1 is +20.2 vs the middle   top-heavy
    friends settings 10 teams   slot 1 is +26.1 vs the middle   top-heavy
    friends settings  8 teams   slot 1 is  -2.8 vs the middle   middle-heavy

An earlier guess that roster shape drove it -- 719 starts 11 with a 5-man
bench, so nearly every pick must become a starter -- was wrong: give 719 ten
teams and it goes top-heavy anyway.

With 8 teams a round consumes only 8 players, the pool stays deep relative to
demand, and the man at the turn is still good; the gain from pick 1 over pick 8
is small, so the 15-pick gaps at the ends dominate and the middle wins. With 10
teams two more elite players leave every round, the drop across round 1
steepens, and holding an elite player outweighs the gaps -- even though those
gaps are LONGER at 19 picks. The talent gradient beats the schedule.

It is also asymmetric: the middle edge at 8 teams is 3-7 points and borderline
noise, the top edge at 10 teams is 20-26 and solid. So at 10+ take the earliest
slot offered; at 8 take the middle but don't pay for it.
"""
from __future__ import annotations

import argparse
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bakeoff import draft, weekly_score          # noqa: E402
from ff import board as board_mod                # noqa: E402
from ff import starts as starts_mod              # noqa: E402
from ff.leagues import load_all                  # noqa: E402
from ff.projections import fetch                 # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("league")
    ap.add_argument("--seeds", type=int, default=25)
    args = ap.parse_args()

    leagues, _ = load_all()
    L = next((x for x in leagues if x.name == args.league), None)
    if L is None:
        raise SystemExit(f"no league {args.league!r}")

    rows, _ = board_mod.build(fetch(L), L)
    byes = starts_mod.bye_by_team()
    rounds = L.starter_slots + L.bench

    print(f"\n{L.name}: {L.teams} teams, {rounds} rounds, "
          f"{args.seeds} drafts per slot\n")
    res = {}
    for slot in range(1, L.teams + 1):
        scores = [weekly_score(draft(L, rows, slot, "board", seed), L, byes)
                  for seed in range(args.seeds)]
        res[slot] = scores
        print(f"  slot {slot} done", flush=True)

    allv = [v for s in res.values() for v in s]
    grand = statistics.mean(allv)
    print(f"\n{'slot':>5}{'mean':>10}{'vs avg':>9}{'std err':>9}   spread of outcomes")
    ranked = sorted(res.items(), key=lambda kv: -statistics.mean(kv[1]))
    for slot, sc in ranked:
        m = statistics.mean(sc)
        se = statistics.stdev(sc) / (len(sc) ** 0.5) if len(sc) > 1 else 0.0
        lo, hi = min(sc), max(sc)
        print(f"{slot:>5}{m:>10.1f}{m - grand:>+9.1f}{se:>9.1f}   {lo:.0f} to {hi:.0f}")

    best, worst = ranked[0], ranked[-1]
    gap = statistics.mean(best[1]) - statistics.mean(worst[1])
    pooled = statistics.mean(
        [statistics.stdev(sc) / (len(sc) ** 0.5) for sc in res.values()])
    print(f"\n  best slot {best[0]} beats worst slot {worst[0]} by {gap:.1f} points")
    print(f"  typical standard error per slot: {pooled:.1f}")
    if gap < 2 * pooled:
        print("  -> that gap is INSIDE the noise. Treat the slots as equivalent;\n"
              "     pick on preference, not on this table.")
    else:
        print("  -> the gap clears the noise, so the ordering is real.")


if __name__ == "__main__":
    main()
