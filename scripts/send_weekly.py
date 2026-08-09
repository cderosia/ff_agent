#!/usr/bin/env python3
"""Build and email the weekly report for every configured league.

    python3 scripts/send_weekly.py --dry-run          # write HTML, send nothing
    python3 scripts/send_weekly.py                    # send
    python3 scripts/send_weekly.py --replay 2025 10   # a past week, for testing

Intended to run from cron on Tuesday and Wednesday nights.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ff import board as board_mod        # noqa: E402
from ff import draft as draft_mod        # noqa: E402
from ff import notify, trades, weekly    # noqa: E402
from ff.leagues import load_all          # noqa: E402
from ff.names import key                 # noqa: E402
from ff.projections import fetch         # noqa: E402

OUT = ROOT / "reports"


def gather(L, cfg, season, week, replay):
    blob = json.loads((ROOT / "data" / "raw" / "sleeper_players.json").read_text())
    league_id = L.raw.get("previous_league_id") if replay else L.league_id
    rows, _ = board_mod.build(fetch(), L)
    by_key = {key(r["name"], r["position"]): r for r in rows}

    rs = requests.get(f"https://api.sleeper.app/v1/league/{league_id}/rosters",
                      timeout=30).json()
    me = next((r for r in rs if r.get("owner_id") == cfg.get("owner_id")), {})

    def to_rows(ids):
        out = []
        for pid in ids or []:
            p = blob.get(pid) or {}
            r = by_key.get(key(f"{p.get('first_name','')} {p.get('last_name','')}".strip(),
                               p.get("position")))
            if r:
                out.append(r)
        return out

    mine = to_rows(me.get("players"))
    taken = set()
    for r in rs:
        for pid in r.get("players") or []:
            p = blob.get(pid) or {}
            taken.add(key(f"{p.get('first_name','')} {p.get('last_name','')}".strip(),
                          p.get("position")))

    trend = weekly.usage_trend(season, week - 1)
    trend["k"] = [key(n, p) for n, p in zip(trend.player_display_name, trend.position)]
    ppg = weekly.recent_points(season, week - 1, L.scoring)
    trend["ppg"] = trend.k.map(ppg).fillna(0.0)
    heat = {} if replay else weekly.heat_rank(blob)

    base = draft_mod.lineup_value(mine, L, L.replacement)
    claims, movers = [], []
    for r in trend.itertuples():
        if r.k in taken or r.snap_pct_recent <= 0.25:
            continue
        row = by_key.get(r.k)
        if row is None:
            continue
        gain = draft_mod.lineup_value(mine + [row], L, L.replacement) - base
        call, why = weekly.claim_call(gain, heat.get(r.k), L.waiver_style, not replay)
        if call == "bid":
            budget = int((cfg.get("manual") or {}).get("faab_budget", 100))
            rec_bid = weekly.faab_bid(len(claims), budget, max(1, 15 - week),
                                      heat.get(r.k) is not None)
            why = f"{why} · open ~${rec_bid}"
        rec = {"name": row["name"], "pos": row["pos_rank"], "gain": gain,
               "ppg": r.ppg, "snap": r.snap_pct_recent, "dsnap": r.snap_delta,
               "tgt": r.targets_recent, "dtgt": r.tgt_delta,
               "call": call, "why": why}
        movers.append(rec)
        if gain > 0:
            claims.append(rec)
    claims.sort(key=lambda c: -c["gain"])
    movers.sort(key=lambda c: -c["dsnap"])

    fair = []
    if mine:
        _, others = trades.league_rosters(league_id, blob, by_key, cfg.get("owner_id"))
        fair = trades.find(mine, others, L, min_my_gain=8, min_their_gain=8,
                           rank="fair", limit=4)
    drops = [{"name": r["name"], "pos": r["pos_rank"], "vorp": r["vorp"]}
             for r in sorted(mine, key=lambda r: r["vorp"])[:5]]
    return claims, movers, fair, drops


def build_lineup_email(L, cfg, season, week, replay, fair):
    """Wednesday email: waivers have cleared, so lead with the lineup."""
    from ff import lineup as lineup_mod
    blob = json.loads((ROOT / "data" / "raw" / "sleeper_players.json").read_text())
    league_id = L.raw.get("previous_league_id") if replay else L.league_id
    rows, _ = board_mod.build(fetch(), L)
    by_key = {key(r["name"], r["position"]): r for r in rows}
    mine, _ = trades.league_rosters(league_id, blob, by_key, cfg.get("owner_id"))

    wp = lineup_mod.weekly_points(week, L.scoring)
    for p in mine:
        pts, n, sp = wp.get(key(p["name"], p["position"]), (0.0, 0, 0.0))
        p["week_points"] = round(pts, 1)
        p["status"], p["why"] = lineup_mod.availability(
            blob, p["name"], p["position"], p.get("team"), week, pts)
    filled, bench = lineup_mod.optimize(mine, L)
    calls = lineup_mod.close_calls(filled, bench, L)
    return notify.render_lineup_email(L, week, filled, bench, calls, fair,
                                      lineup_mod.outdoor_games(week), replay)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--replay", nargs=2, type=int, metavar=("SEASON", "WEEK"))
    ap.add_argument("--league")
    ap.add_argument("--kind", choices=["tue","wed"], default="tue")
    args = ap.parse_args()

    replay = args.replay is not None
    season, week = args.replay if replay else (2026, 1)

    cfgs = {c["name"]: c for c in
            yaml.safe_load((ROOT / "leagues" / "leagues.yaml").read_text())["leagues"]}
    leagues, _ = load_all()
    OUT.mkdir(exist_ok=True)

    for L in leagues:
        if args.league and L.name != args.league:
            continue
        cfg = cfgs.get(L.name, {})
        if L.platform != "sleeper" or not cfg.get("owner_id"):
            print(f"  skip {L.name}: needs Sleeper + owner_id")
            continue
        try:
            claims, movers, fair, drops = gather(L, cfg, season, week, replay)
        except Exception as e:
            print(f"  FAIL {L.name}: {type(e).__name__}: {e}")
            continue

        if args.kind == "wed":
            html = build_lineup_email(L, cfg, season, week, replay, fair)
        else:
            html = notify.render_email(L, week, claims, movers, fair, drops, replay)
        path = OUT / f"{L.name}-w{week}-{args.kind}.html"
        path.write_text(html)

        if args.dry_run:
            print(f"  {L.name}: {len(claims)} adds, {len(fair)} trades "
                  f"-> {path.relative_to(ROOT)} (not sent)")
        else:
            if args.kind == "wed":
                subject = f"Week {week} lineup · {L.name}"
            else:
                top = claims[0]["name"] if claims else "no adds"
                subject = f"Week {week} · {L.name} — {top}"
            print(f"  {L.name}: {notify.send(subject, html)}")


if __name__ == "__main__":
    main()
