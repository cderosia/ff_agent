#!/usr/bin/env python3
"""Weekly report — Tuesday (waivers) and Wednesday (lineups/trades).

    python3 scripts/weekly_report.py --league freinds-keeper --kind tue
    python3 scripts/weekly_report.py --league freinds-keeper --kind tue \
        --replay-season 2025 --replay-week 10

Replay mode rebuilds a past week using only data available before it, which is
the only honest way to see what the report looks like in-season while it's still
preseason.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime

import requests
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from ff import board, draft, weekly            # noqa: E402
from ff.leagues import load_all                # noqa: E402
from ff.names import key                       # noqa: E402
from ff.projections import fetch               # noqa: E402
from ff.stats import SLOT_ELIGIBILITY          # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "reports"


def player_name(blob, pid):
    p = blob.get(pid) or {}
    return f"{p.get('first_name','')} {p.get('last_name','')}".strip(), p.get("position")


def build(L, cfg, season, week, live):
    """Assemble every fact the report needs."""
    blob = json.loads((ROOT / "data" / "raw" / "sleeper_players.json").read_text())
    league_id = L.league_id if live else L.raw["previous_league_id"]

    proj = fetch()
    rows, _ = board.build(proj, L)
    by_key = {key(r["name"], r["position"]): r for r in rows}

    # my roster
    rs = requests.get(f"https://api.sleeper.app/v1/league/{league_id}/rosters",
                      timeout=30).json()
    me = next((r for r in rs if r.get("owner_id") == cfg.get("owner_id")), None)
    my_ids = (me or {}).get("players") or []
    mine = []
    for pid in my_ids:
        n, pos = player_name(blob, pid)
        r = by_key.get(key(n, pos))
        if r:
            mine.append(r)

    taken_ids = set()
    for r in rs:
        taken_ids |= set(r.get("players") or [])
    taken = set()
    for pid in taken_ids:
        n, pos = player_name(blob, pid)
        taken.add(key(n, pos))

    # usage, strictly from weeks before the target week
    trend = weekly.usage_trend(season, week - 1)
    trend["k"] = [key(n, p) for n, p in zip(trend.player_display_name, trend.position)]
    ppg = weekly.recent_points(season, week - 1, L.scoring)
    trend["ppg"] = trend.k.map(ppg).fillna(0.0)
    tmap = {r.k: r for r in trend.itertuples()}

    # candidates = not rostered anywhere, with a real snap share
    fa = [r for r in trend.itertuples()
          if r.k not in taken and r.snap_pct_recent > 0.25]

    # rank by what they'd add to YOUR lineup, using season projections
    repl = L.replacement
    base = draft.lineup_value(mine, L, repl)
    cands = []
    for r in fa:
        row = by_key.get(r.k)
        if row is None:
            continue
        gain = draft.lineup_value(mine + [row], L, repl) - base
        cands.append({
            "name": row["name"], "pos": row["pos_rank"], "position": row["position"],
            "gain": round(gain, 1), "vorp": row["vorp"], "ppg": round(r.ppg, 1),
            "snap": r.snap_pct_recent, "dsnap": r.snap_delta,
            "tgt": r.targets_recent, "dtgt": r.tgt_delta,
            "car": r.carries_recent, "dcar": r.carry_delta,
        })
    cands.sort(key=lambda c: (-c["gain"], -c["ppg"]))

    # pure usage movers, need-agnostic: intelligence, not a recommendation
    movers = sorted(fa, key=lambda r: -(r.snap_delta * 100 + r.tgt_delta * 3
                                        + r.carry_delta * 2))[:8]

    # drop candidates: rostered players contributing least
    drops = sorted(mine, key=lambda r: r["vorp"])[:5]

    budget = (me or {}).get("settings", {}).get("waiver_budget_used", 0)
    total = (L.raw.get("settings") or {}).get("waiver_budget", 100)
    gaps = {s: c for s, c in draft.roster_gaps(mine, L).items()
            if s not in ("K", "DST")}      # not modelled; drafted off-list
    return dict(mine=mine, cands=cands, movers=movers, drops=drops, tmap=tmap,
                budget_left=total - budget, total=total, gaps=gaps)


def render_tuesday(L, d, season, week, live):
    weeks_left = max(1, 15 - week)
    n = "\n"
    out = [f"# {L.name} — week {week} waivers", ""]
    tag = "" if live else f" · **REPLAY of {season} week {week}** (data through week {week-1} only)"
    out.append(f"*generated {datetime.now():%a %d %b %H:%M}{tag}*")
    out.append("")
    out.append(f"**FAAB left** ${d['budget_left']} of ${d['total']} · "
               f"**roster gaps**: " +
               (", ".join(f"{s}×{c}" for s, c in d["gaps"].items()) or "starters full"))
    out.append("")

    out.append("## Claim these")
    out.append("")
    out.append("Ranked by what they add to *your* starting lineup, not by raw upside — "
               "a great player at a position you've already filled is worth nothing this week.")
    out.append("")
    useful = [c for c in d["cands"] if c["gain"] > 0]
    if not useful:
        out.append("_Nothing on the wire improves your starting lineup. "
                   "Save the FAAB._")
    else:
        out.append("| | player | pos | adds | ppg | snap% | Δsnap | tgt | Δtgt | bid |")
        out.append("|---:|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for i, c in enumerate(useful[:6]):
            bid = weekly.faab_bid(i, d["budget_left"], weeks_left, 0.0)
            gain = f"**+{c['gain']:.0f}**"
            out.append(f"| {i+1} | {c['name']} | {c['pos']} | {gain} | {c['ppg']:.1f} | "
                       f"{c['snap']*100:.0f}% | {c['dsnap']*100:+.0f} | {c['tgt']:.1f} | "
                       f"{c['dtgt']:+.1f} | ${bid} |")
        out.append("")
        top = useful[0]
        out.append(f"**Priority: {top['name']}.** Adds {top['gain']:.0f} points to your "
                   f"projected starting lineup.")
        bench = [c for c in d["cands"] if c["gain"] <= 0][:3]
        if bench:
            out.append("")
            out.append("Producing but *don't* improve your lineup — you're already "
                       "covered at their position, so no bid: "
                       + ", ".join(f"{c['name']} ({c['pos']}, {c['ppg']:.1f} ppg)"
                                   for c in bench) + ".")
    out.append("")

    out.append("## Usage risers — market hasn't caught up")
    out.append("")
    out.append("Opportunity moves before production does. These are the biggest "
               "snap/target gains among unrostered players, regardless of whether "
               "they fit your roster — stash-or-ignore intelligence.")
    out.append("")
    out.append("| player | pos | snap% | Δsnap | tgt | Δtgt | car | Δcar | ppg |")
    out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in d["movers"]:
        out.append(f"| {r.player_display_name} | {r.position} | {r.snap_pct_recent*100:.0f}% | "
                   f"{r.snap_delta*100:+.0f} | {r.targets_recent:.1f} | {r.tgt_delta:+.1f} | "
                   f"{r.carries_recent:.1f} | {r.carry_delta:+.1f} | {r.ppg:.1f} |")
    out.append("")

    out.append("## Droppable")
    out.append("")
    out.append("| player | pos | vorp | note |")
    out.append("|---|---|---:|---|")
    for r in d["drops"]:
        note = "below replacement" if r["vorp"] < 0 else "lowest value on roster"
        out.append(f"| {r['name']} | {r['pos_rank']} | {r['vorp']:.0f} | {note} |")
    out.append("")
    return n.join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", required=True)
    ap.add_argument("--kind", choices=["tue", "wed"], default="tue")
    ap.add_argument("--replay-season", type=int)
    ap.add_argument("--replay-week", type=int)
    args = ap.parse_args()

    cfgs = {c["name"]: c for c in
            yaml.safe_load((ROOT / "leagues" / "leagues.yaml").read_text())["leagues"]}
    leagues, _ = load_all()
    L = next((x for x in leagues if x.name == args.league), None)
    if L is None:
        sys.exit(f"no league {args.league!r}")

    live = args.replay_week is None
    season = args.replay_season or 2026
    week = args.replay_week or 1

    d = build(L, cfgs.get(L.name, {}), season, week, live)
    body = render_tuesday(L, d, season, week, live)
    OUT.mkdir(exist_ok=True)
    path = OUT / f"{L.name}-w{week}-{args.kind}.md"
    path.write_text(body)
    print(f"  -> {path.relative_to(ROOT)}")
    print(body[:1800])


if __name__ == "__main__":
    main()
