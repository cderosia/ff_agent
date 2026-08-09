#!/usr/bin/env python3
"""Generate a draft board for every configured league.

    python3 scripts/build_boards.py              # all leagues -> boards/
    python3 scripts/build_boards.py --league 719
    python3 scripts/build_boards.py --refresh    # bypass the projection cache
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from ff import board                    # noqa: E402
from ff.leagues import load_all         # noqa: E402
from ff.projections import fetch        # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "boards"


def render(league, rows, meta) -> str:
    ppr = league.scoring.get("receptions", 0.0)
    fmt = {0.0: "standard", 0.5: "half-PPR", 1.0: "PPR"}.get(ppr, f"{ppr}/rec")
    slots = " ".join(f"{k}×{v}" for k, v in league.starters.items())
    L = []
    L.append(f"# {league.name} — draft board")
    L.append("")
    L.append(f"*{league.teams}-team {fmt} on {league.platform} · "
             f"generated {datetime.now():%Y-%m-%d %H:%M}*")
    L.append("")
    L.append(f"- **Starters**: {slots} (+{league.bench} bench)")
    L.append(f"- **Scoring**: pass TD {league.scoring.get('pass_td',0):g} · "
             f"rush/rec TD {league.scoring.get('rush_td',0):g} · "
             f"{ppr:g} per reception")
    L.append(f"- **Replacement level**: " +
             " · ".join(f"{k} {v:.0f}" for k, v in sorted(league.replacement.items())))
    L.append(f"- **Market**: {meta['source']} "
             f"({meta['matched']} of {meta['market_size']} players matched)")
    if league.notes:
        L.append(f"- **Notes**: {league.notes}")
    L.append("")

    L.append("## Value vs. the room")
    L.append("")
    L.append("`edge` = market rank − your rank, computed on the shared player set. "
             "Positive means he lasts longer than he should.")
    L.append("")
    L.append("| Draft later than market | pos | you | market | edge |")
    L.append("|---|---|---:|---:|---:|")
    for r in board.values(rows, 12):
        L.append(f"| {r['name']} | {r['pos_rank']} | {r['your_rank']} | {r['adp_rank']} | +{r['edge']} |")
    L.append("")
    L.append("| Let the room overpay | pos | you | market | edge |")
    L.append("|---|---|---:|---:|---:|")
    for r in board.reaches(rows, 12):
        L.append(f"| {r['name']} | {r['pos_rank']} | {r['your_rank']} | {r['adp_rank']} | {r['edge']} |")
    L.append("")

    L.append("## Full board")
    L.append("")
    L.append("| # | player | pos | proj | vorp | market | edge | src | spread |")
    L.append("|---:|---|---|---:|---:|---:|---:|:--:|---:|")
    for r in rows[:180]:
        edge = f"{r['edge']:+d}" if r["edge"] is not None else "—"
        mkt = r["adp_rank"] if r["adp_rank"] else "—"
        src = "2" if r.get("n_sources", 1) > 1 else "1"
        sp = f"{r['spread']:.0f}" if r.get("spread") else "—"
        L.append(f"| {r['vbd_rank']} | {r['name']} | {r['pos_rank']} | "
                 f"{r['points']:.0f} | {r['vorp']:.0f} | {mkt} | {edge} | {src} | {sp} |")
    L.append("")
    L.append("## Where the projections disagree")
    L.append("")
    L.append("Both sources scored under this league's rules. Big gaps mean low "
             "confidence — treat these as range bets, not point estimates.")
    L.append("")
    L.append("| player | pos | you | espn | sleeper | disagree |")
    L.append("|---|---|---:|---:|---:|---:|")
    contested = [r for r in rows if r.get("n_sources", 1) > 1 and r["vbd_rank"] <= 120]
    for r in sorted(contested, key=lambda r: -r.get("rel_spread", 0))[:12]:
        ps = r["per_source"]
        L.append(f"| {r['name']} | {r['pos_rank']} | {r['vbd_rank']} | "
                 f"{ps.get('espn','—')} | {ps.get('sleeper','—')} | {r['rel_spread']*100:.0f}% |")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", help="only this league (by name)")
    ap.add_argument("--refresh", action="store_true", help="bypass projection cache")
    args = ap.parse_args()

    proj = fetch(force=args.refresh)
    leagues, errors = load_all()
    if args.league:
        leagues = [l for l in leagues if l.name == args.league]
        if not leagues:
            sys.exit(f"no league named {args.league!r}")

    OUT.mkdir(exist_ok=True)
    print(f"projections: {len(proj)} players\n")
    for L in leagues:
        rows, meta = board.build(proj, L)
        path = OUT / f"{L.name}.md"
        path.write_text(render(L, rows, meta))
        print(f"  {L.name:16} {L.teams:>2}tm {L.platform:8} "
              f"{meta['source']:10} match {meta['match_rate']:.0%}  -> {path.relative_to(ROOT)}")

    for name, err in errors:
        print(f"  SKIP {name}: {err[:70]}")


if __name__ == "__main__":
    main()
