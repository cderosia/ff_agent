#!/usr/bin/env python3
"""Trace one player end to end, through every step of the ranking.

    python3 scripts/explain.py "Trey McBride"
    python3 scripts/explain.py "J.K. Dobbins" --league family

Shows exactly how raw projections become a rank, and where the number you see
on the board actually comes from.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from ff import board as board_mod        # noqa: E402
from ff.leagues import load_all          # noqa: E402
from ff.names import key, normalize      # noqa: E402
from ff.projections import fetch         # noqa: E402
from ff.stats import SLOT_ELIGIBILITY    # noqa: E402

BAR = "─" * 74


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("player")
    ap.add_argument("--league", help="only this league")
    args = ap.parse_args()

    proj = fetch()
    want = normalize(args.player)
    match = [p for p in proj if want in normalize(p["name"])]
    if not match:
        sys.exit(f"no player matching {args.player!r}")
    p = match[0]

    print(f"\n{BAR}\n  {p['name']}  ({p['position']})\n{BAR}")

    # ---- 1. projections ---------------------------------------------------
    print("\n1. PROJECTED STAT LINE — blended, before any league's scoring")
    print(f"   sources: {', '.join(p['sources'])}")
    lines = p.get("source_lines") or {}
    stats = sorted(p["stats"].items(), key=lambda kv: -abs(kv[1]))
    for stat, val in stats:
        # "—" means the source doesn't publish that stat at all, which is not the
        # same as projecting zero -- the blend averages only over sources that
        # report it, so the distinction matters for reading the number.
        per = "  ".join(
            f"{s}=" + (f"{lines[s][stat]:.1f}" if stat in lines[s] else "—")
            for s in sorted(lines))
        print(f"     {stat:12} {val:8.1f}   ({per})")

    leagues, _ = load_all()
    if args.league:
        leagues = [l for l in leagues if l.name == args.league]

    for L in leagues:
        rows, meta = board_mod.build(proj, L)
        row = next((r for r in rows if key(r["name"], r["position"])
                    == key(p["name"], p["position"])), None)
        if row is None:
            continue

        ppr = L.scoring.get("receptions", 0.0)
        fmt = {0.0: "standard", 0.5: "half-PPR", 1.0: "PPR"}.get(ppr, f"{ppr}/rec")
        print(f"\n{BAR}\n  {L.name}   ({L.teams}-team {fmt}, {L.platform})\n{BAR}")

        # ---- 2. scoring ---------------------------------------------------
        print("\n2. SCORED under THIS league's rules")
        contrib = []
        for stat, val in p["stats"].items():
            c = L.scoring.get(stat, 0.0)
            if c and val:
                contrib.append((stat, val, c, val * c))
        contrib.sort(key=lambda x: -abs(x[3]))
        for stat, val, coef, pts in contrib:
            print(f"     {stat:12} {val:7.1f} × {coef:<6g} = {pts:8.1f}")
        print(f"     {'':12} {'':7}   {'':6}   {'-'*8}")
        print(f"     {'TOTAL':12} {'':7}   {'':6}   {row['points']:8.1f} projected points")

        # ---- 3. replacement ------------------------------------------------
        pos = row["position"]
        starts = sum(c for s, c in L.starters.items()
                     if pos in SLOT_ELIGIBILITY.get(s, {s}))
        used = L.starters_used.get(pos, 0)
        print(f"\n3. REPLACEMENT LEVEL for {pos}")
        print(f"     {L.teams} teams × {starts} slots {pos} can fill = up to "
              f"{L.teams*starts} spots")
        print(f"     simulating who actually starts league-wide: {used} {pos}s start")
        print(f"     so replacement = the {used+1}th best {pos} = "
              f"{row['replacement']:.1f} points")
        print(f"     (this is the single biggest lever in the whole model)")

        # ---- 4. VORP -------------------------------------------------------
        print(f"\n4. VALUE OVER REPLACEMENT")
        print(f"     {row['points']:.1f} − {row['replacement']:.1f} = "
              f"{row['vorp']:.1f} VORP")
        print(f"     -> your rank #{row['vbd_rank']} of {len(rows)}  ({row['pos_rank']})")

        # ---- 5. market -----------------------------------------------------
        print(f"\n5. MARKET — {meta['source']}")
        if row.get("adp_rank"):
            print(f"     raw ADP {row['adp']:.1f}")
            print(f"     re-ranked inside the {meta['matched']} players present in BOTH")
            print(f"     your rank there: #{row['your_rank']}   market rank: #{row['adp_rank']}")
            print(f"\n6. EDGE = market {row['adp_rank']} − you {row['your_rank']} = "
                  f"{row['edge']:+d}")
            if row["edge"] > 0:
                print(f"     positive: the room lets him fall {row['edge']} spots past "
                      f"where you'd take him")
            elif row["edge"] < 0:
                print(f"     negative: you'd have to reach {abs(row['edge'])} spots "
                      f"earlier than he's worth")
        else:
            print("     not in this market's player pool — no edge computed")

    print(f"\n{BAR}")
    print("  Edge is a difference of RANKS, not points. Ranks are dense in the")
    print("  middle rounds and sparse at the top, so +30 late is a smaller real")
    print("  gain than +30 early. Use VORP for magnitude, edge for timing.")
    print(f"{BAR}\n")


if __name__ == "__main__":
    main()
