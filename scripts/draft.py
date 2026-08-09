#!/usr/bin/env python3
"""Live draft assistant.

    python3 scripts/draft.py --league 719              # poll the live draft
    python3 scripts/draft.py --league 719 --once       # single snapshot
    python3 scripts/draft.py --league freinds-keeper --replay 40
                                                       # dry run: replay last
                                                       # year's draft to pick 40

Shows what's left, where your roster is thin, and who to take -- separating
"best value on the board" from "best for your team right now".
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

import requests
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from ff import board, draft                    # noqa: E402
from ff.leagues import _env, load_all          # noqa: E402
from ff.names import key                       # noqa: E402
from ff.projections import fetch               # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


def my_espn_team(league_id: str, cookies: dict) -> int | None:
    """Find your teamId by matching the SWID cookie against league members."""
    swid = _env("ESPN_SWID")
    url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026"
           f"/segments/0/leagues/{league_id}?view=mTeam")
    d = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                     cookies=cookies, timeout=25).json()
    me = next((m["id"] for m in d.get("members", [])
               if m.get("id", "").upper() == swid.upper()), None)
    for t in d.get("teams", []):
        if me and me in (t.get("owners") or []):
            return t["id"]
    return None


def get_picks(L, cfg, id_to_player, replay=None):
    """Current picks, or a slice of last season's draft when replaying."""
    if replay is not None:
        prev = L.raw.get("previous_league_id")
        lg = requests.get(f"https://api.sleeper.app/v1/league/{prev}", timeout=20).json()
        return draft.sleeper_picks(lg["draft_id"])[:replay]
    if L.platform == "sleeper":
        return draft.sleeper_picks(L.raw["draft_id"])
    if L.platform == "espn":
        cookies = {"espn_s2": _env("ESPN_S2"), "SWID": _env("ESPN_SWID")}
        return draft.espn_picks(L.league_id, cookies, id_to_player)
    return []


def render(L, rows, picks, me, replay):
    taken = {key(p["name"], p["position"]) for p in picks if p["name"]}
    by_key = {key(r["name"], r["position"]): r for r in rows}
    mine = [by_key[key(p["name"], p["position"])] for p in picks
            if p.get("by") == me and key(p["name"], p["position"]) in by_key]

    out = []
    hdr = f" {L.name} — pick {len(picks)+1} "
    out.append(f"\n\033[1m{hdr:=^78}\033[0m")
    if replay is not None:
        out.append("  \033[33m[REPLAY of last season's draft — not live]\033[0m")

    gaps = draft.roster_gaps(mine, L)
    out.append(f"\n  YOUR ROSTER ({len(mine)}): " +
               (", ".join(f"{p['name']} ({p['pos_rank']})" for p in mine) or "—"))
    out.append(f"  STILL NEED : " +
               (" ".join(f"{s}×{n}" for s, n in gaps.items()) or "starters full"))

    recs = draft.recommend(rows, L, taken, mine, limit=10)
    out.append(f"\n  \033[1mBEST FOR YOU\033[0m (marginal lineup value)")
    out.append(f"    {'player':22}{'pos':6}{'vorp':>6}{'mkt':>6}{'edge':>6}{'gain':>7}")
    for r in recs:
        edge = f"{r['edge']:+d}" if r.get("edge") is not None else "—"
        mkt = r.get("adp_rank") or "—"
        out.append(f"    {r['name'][:22]:22}{r['pos_rank']:6}{r['vorp']:>6.0f}"
                   f"{str(mkt):>6}{edge:>6}{r['marginal']:>7.0f}")

    avail = [r for r in rows if key(r["name"], r["position"]) not in taken]
    val = [r for r in avail if r.get("edge") is not None and r["vbd_rank"] <= 160]
    val.sort(key=lambda r: -r["edge"])
    out.append(f"\n  \033[1mBEST VALUE vs MARKET\033[0m (falling past their price)")
    for r in val[:6]:
        out.append(f"    {r['name'][:22]:22}{r['pos_rank']:6}{r['vorp']:>6.0f}"
                   f"{r['adp_rank']:>6}{r['edge']:>+6}")

    nxt = [r for r in avail if r.get("adp_rank")]
    nxt.sort(key=lambda r: r["adp_rank"])
    out.append("\n  LIKELY GONE SOON: " +
               ", ".join(f"{r['name']}({r['pos_rank']})" for r in nxt[:6]))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", required=True)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--replay", type=int, metavar="N")
    ap.add_argument("--interval", type=float, default=8.0)
    args = ap.parse_args()

    cfgs = {c["name"]: c for c in
            yaml.safe_load((ROOT / "leagues" / "leagues.yaml").read_text())["leagues"]}
    proj = fetch()
    leagues, _ = load_all()
    L = next((x for x in leagues if x.name == args.league), None)
    if L is None:
        sys.exit(f"no league {args.league!r}")
    cfg = cfgs.get(L.name, {})
    rows, _ = board.build(proj, L)
    id_to_player = {p["espn_id"]: p for p in proj if p.get("espn_id")}

    if L.platform == "espn":
        me = my_espn_team(L.league_id,
                          {"espn_s2": _env("ESPN_S2"), "SWID": _env("ESPN_SWID")})
    else:
        me = cfg.get("owner_id")
    if me is None:
        print("  warning: couldn't identify your team; roster view will be empty")

    while True:
        picks = get_picks(L, cfg, id_to_player, args.replay)
        print(render(L, rows, picks, me, args.replay))
        if args.once or args.replay is not None:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
