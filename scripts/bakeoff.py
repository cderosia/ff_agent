#!/usr/bin/env python3
"""Draft the same league four ways and score the rosters. The only validation.

Every scoring change in this repo is an argument that the board picks better
players. This settles it by drafting: each strategy runs from every slot, three
seeds each, against a room drafting off jittered ADP. Rosters are scored as
starting-lineup points summed week by week with byes removed, so a team that
stacks four players on one bye is penalised the way it would be in September.

    python3 scripts/bakeoff.py [league ...]

Strategies:
    board      what scripts/draft_server.py recommends
    caps_adp   follow ADP, but respect roster construction limits
    adp        follow ADP off a cliff
    vorp       highest VORP available -- the pre-2026 tiebreak

READ THIS BEFORE QUOTING THE NUMBERS. Rosters are scored with the same
projections the board optimises, so the board is being graded on its own
objective and the comparison flatters it. This measures roster CONSTRUCTION --
whether the board turns a set of projections into a good lineup -- and says
nothing about whether those projections are right. Beating ADP here is not
evidence of beating your league.
"""
from __future__ import annotations

import collections
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ff import board as board_mod        # noqa: E402
from ff import draft as draft_mod        # noqa: E402
from ff import starts as starts_mod      # noqa: E402
from ff import vbd                       # noqa: E402
from ff.leagues import load_all          # noqa: E402
from ff.names import key                 # noqa: E402
from ff.projections import fetch         # noqa: E402

SEEDS = (1, 2, 3)
STRATEGIES = ("board", "caps_adp", "adp", "vorp")


def weekly_score(roster, league, byes) -> float:
    """Starting-lineup points summed over the fantasy season, byes removed.

    An unfilled slot scores nothing, which is the point: it's the zero you
    actually eat that week for stacking byes or ignoring a position.
    """
    total = 0.0
    for wk in starts_mod.FANTASY_WEEKS:
        live = [p for p in roster
                if starts_mod.bye_for(p.get("team"), byes) != wk]
        filled = draft_mod._assign(live, league)
        for slot in league.starters:
            if slot in ("K", "DST"):
                continue
            total += sum(p["points"] for p in filled.get(slot, [])) / 17.0
    return total


def draft(league, rows, slot, strategy, seed) -> list[dict]:
    rnd = random.Random(seed)
    rounds = league.starter_slots + league.bench
    order = (sorted([r for r in rows if r.get("adp")], key=lambda r: r["adp"])
             + [r for r in rows if not r.get("adp")])
    mine_at = {(rd - 1) * league.teams
               + (slot if rd % 2 else league.teams - slot + 1)
               for rd in range(1, rounds + 1)}
    picks = sorted(mine_at)
    caps = draft_mod.roster_max(league)
    taken, mine = set(), []

    for pk in range(1, rounds * league.teams + 1):
        if pk in mine_at:
            i = picks.index(pk)
            nxt = picks[i + 1] if i + 1 < len(picks) else None
            avail = [r for r in rows if key(r["name"], r["position"]) not in taken]
            held = collections.Counter(p["position"] for p in mine)
            legal = [r for r in avail
                     if held.get(r["position"], 0) < caps.get(r["position"], 99)]
            legal = legal or avail
            if strategy == "board":
                p = draft_mod.recommend(rows, league, taken, mine, limit=60,
                                        next_pick=pk, following_pick=nxt)[0]
            elif strategy == "caps_adp":
                p = next(r for r in order if r in legal)
            elif strategy == "adp":
                p = next(r for r in order
                         if key(r["name"], r["position"]) not in taken)
            elif strategy == "vorp":
                p = max(avail, key=lambda r: r["vorp"])
            else:
                raise SystemExit(f"unknown strategy {strategy}")
            mine.append(p)
            taken.add(key(p["name"], p["position"]))
        else:
            # The room drafts off ADP with a wobble, so it reaches and slides
            # the way a real room does rather than draining a fixed list.
            pool = [r for r in order if key(r["name"], r["position"]) not in taken]
            if not pool:
                break
            k = min(len(pool) - 1, max(0, int(rnd.gauss(0, 2.5))))
            taken.add(key(pool[k]["name"], pool[k]["position"]))
    return mine


def main():
    wanted = sys.argv[1:]
    leagues, _ = load_all()
    if wanted:
        leagues = [l for l in leagues if l.name in wanted]
    byes = starts_mod.bye_by_team()

    for L in leagues:
        rows, _ = board_mod.build(fetch(L), L)
        print(f"\n=== {L.name}  ({L.teams} teams, "
              f"{L.starter_slots + L.bench} rounds)")
        print(f"    weekly starting-lineup points, "
              f"{len(list(starts_mod.FANTASY_WEEKS))} weeks, byes applied")
        res = {}
        for strat in STRATEGIES:
            scores = [weekly_score(draft(L, rows, slot, strat, seed), L, byes)
                      for slot in range(1, L.teams + 1) for seed in SEEDS]
            res[strat] = sum(scores) / len(scores)
        base = res["adp"]
        for s, v in sorted(res.items(), key=lambda kv: -kv[1]):
            print(f"      {s:10} {v:8.1f}   {v - base:+7.1f} vs following ADP")


if __name__ == "__main__":
    main()
