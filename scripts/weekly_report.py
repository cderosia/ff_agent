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

from ff import board, draft, lineup, rosters, weekly, winprob            # noqa: E402
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

    # Every team's roster, from whichever platform this league lives on. Win
    # probability needs the whole field, not just yours.
    all_teams = rosters.all_teams(L, week, blob)
    me_team = next((t for t in all_teams if t.mine), None)
    mine = []
    for p in (me_team.players if me_team else []):
        r = by_key.get(key(p["name"], p["position"]))
        if r:
            mine.append(r)

    taken = {key(p["name"], p["position"])
             for t in all_teams for p in t.players}

    # Usage, strictly from weeks before the target week. In week 1 there is no
    # such data -- no games have been played -- so the waiver half of the
    # report stands down rather than failing. The odds half still works, and a
    # week-1 report that says "no usage yet" is more useful than no report.
    try:
        trend = weekly.usage_trend(season, week - 1)
    except Exception as e:
        trend = None
        usage_note = (f"No usage data yet for {season} week {week} — "
                      f"waiver targets need at least one completed week.")
    else:
        usage_note = None
    if trend is None:
        cands, movers, tmap = [], [], {}
        drops = sorted(mine, key=lambda r: r["vorp"])[:5]
        gaps = {s: c for s, c in draft.roster_gaps(mine, L).items()
                if s not in ("K", "DST")}
        total = int(L.raw.get("faab_budget") or 0)
        odds = None
        try:
            wpts = lineup.weekly_points(week, L.scoring)
            opp = rosters.opponent_of(L, week, all_teams)
            odds = winprob.league_odds(all_teams, wpts, L, opponent=opp, week=week)
        except Exception as e:
            odds = {"error": f"{type(e).__name__}: {e}"}
        return dict(mine=mine, cands=cands, movers=movers, drops=drops, tmap=tmap,
                    budget_left=total, total=total, gaps=gaps, odds=odds,
                    teams=all_teams, usage_note=usage_note)

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
    heat = weekly.heat_rank(blob) if live else {}
    for c in cands:
        c["heat"] = heat.get(key(c["name"], c["position"]))
    cands.sort(key=lambda c: (-c["gain"], -c["ppg"]))

    # pure usage movers, need-agnostic: intelligence, not a recommendation
    movers = sorted(fa, key=lambda r: -(r.snap_delta * 100 + r.tgt_delta * 3
                                        + r.carry_delta * 2))[:8]

    # drop candidates: rostered players contributing least
    drops = sorted(mine, key=lambda r: r["vorp"])[:5]

    total = int(L.raw.get("faab_budget")
                or (L.raw.get("settings") or {}).get("waiver_budget", 0) or 0)
    spent = 0
    if L.platform == "sleeper" and me_team:
        rs = requests.get(f"https://api.sleeper.app/v1/league/{league_id}/rosters",
                          timeout=30).json()
        r = next((x for x in rs if str(x.get("roster_id")) == me_team.team_id), None)
        spent = ((r or {}).get("settings") or {}).get("waiver_budget_used", 0)
    gaps = {s: c for s, c in draft.roster_gaps(mine, L).items()
            if s not in ("K", "DST")}      # not modelled; drafted off-list
    odds = None
    try:
        wpts = lineup.weekly_points(week, L.scoring)
        opp = rosters.opponent_of(L, week, all_teams)
        odds = winprob.league_odds(all_teams, wpts, L, opponent=opp, week=week)
        if odds and odds.get("mode") == "guillotine":
            _price_survival(cands[:12], me_team, all_teams, wpts, L, week,
                            odds, total - spent)
    except Exception as e:                      # a report is worth more than a stat
        odds = {"error": f"{type(e).__name__}: {e}"}

    return dict(mine=mine, cands=cands, movers=movers, drops=drops, tmap=tmap,
                budget_left=total - spent, total=total, gaps=gaps,
                odds=odds, teams=all_teams)


def _price_survival(cands, me_team, all_teams, wpts, L, week, odds, budget_left):
    """In an elimination league, a claim is worth the survival it buys.

    Points added is the wrong currency here: an extra six points means nothing
    if you were clearing the cut anyway, and everything if you weren't. So each
    candidate is re-run through the same simulation with him in the lineup, and
    priced on the survival probability he actually adds.

    The bid is budget x survival bought, which is a heuristic and labelled as
    one -- but it has the right shape. Budget is worth nothing after you are
    eliminated, so a player who moves survival meaningfully is worth a large
    slice of it, and one who moves it not at all is worth a minimum bid however
    good he looks.
    """
    from ff.rosters import Team
    base = odds.get("advance", 0.0)
    others = [t for t in all_teams if not t.mine]
    for c in cands:
        added = Team(team_id=me_team.team_id, name=me_team.name, mine=True,
                     players=me_team.players + [{"name": c["name"],
                                                 "position": c["position"],
                                                 "team": None}])
        try:
            r = winprob.league_odds(others + [added], wpts, L, week=week)
            c["d_survive"] = round(r.get("advance", base) - base, 4)
        except Exception:
            c["d_survive"] = None
        if c["d_survive"] is not None:
            c["bid"] = max(1, int(round(budget_left * max(0.0, c["d_survive"]))))


def render_tuesday(L, d, season, week, live):
    weeks_left = max(1, 15 - week)
    n = "\n"
    out = [f"# {L.name} — week {week} waivers", ""]
    tag = "" if live else f" · **REPLAY of {season} week {week}** (data through week {week-1} only)"
    out.append(f"*generated {datetime.now():%a %d %b %H:%M}{tag}*")
    out.append("")
    out.append(f"**Waivers**: {L.waiver_note} · "
               f"**roster gaps**: " +
               (", ".join(f"{s}×{c}" for s, c in d["gaps"].items()) or "starters full"))
    out.append("")

    o = d.get("odds") or {}
    if o.get("error"):
        out.append(f"> odds unavailable this week — {o['error']}")
        out.append("")
    elif o.get("mode") == "guillotine":
        adv = o["advance"]
        out.append("## Survival")
        out.append("")
        out.append(f"**{adv:.0%} chance you advance** this week "
                   f"({o['eliminated']:.0%} eliminated). Projected "
                   f"**{o['projected_rank']} of {o['of']}** at "
                   f"{o['mine']['proj']} pts.")
        out.append("")
        out.append(f"Cushion over the low team: **{o['median_cushion']:+.0f}** in a "
                   f"median week, **{o['cushion_10th_pct']:+.0f}** in a bad one. "
                   f"The second number is the one that matters — it's what happens "
                   f"when your projections come in low.")
        out.append("")
        out.append("| | team | proj | sd |")
        out.append("|:--|---|---:|---:|")
        for t in o["teams"]:
            out.append(f"| {'**you**' if t['mine'] else ''} | {t['team']} "
                       f"| {t['proj']} | {t['sd']} |")
        out.append("")
        out.append("_Odds come from your league's scoring, not the platform's: every "
                   "team's best legal lineup, each player's spread measured from "
                   "2019-2025 week-to-week variance, simulated 40,000 times. "
                   "Players are treated as independent, so a stacked lineup is a "
                   "little wilder than this says._")
        out.append("")
    elif o.get("mode") == "head_to_head" and o.get("win") is not None:
        out.append("## This week")
        out.append("")
        out.append(f"**{o['win']:.0%} to win** vs {o['opponent']} — "
                   f"{o['mine']['proj']} to {o['opponent_proj']}.")
        out.append("")
    elif o.get("mode") == "field":
        out.append("## This week")
        out.append("")
        out.append(f"Projected **{o['projected_rank']} of {o['of']}** at "
                   f"{o['mine']['proj']} pts · "
                   f"{o['beat_median']:.0%} to beat a median team.")
        out.append("")

    if d.get("usage_note"):
        out.append(f"> {d['usage_note']}")
        out.append("")

    out.append("## Claim these")
    out.append("")
    out.append("Ranked by what they add to *your* starting lineup, not by raw upside — "
               "a great player at a position you've already filled is worth nothing this week.")
    out.append("")
    if any(c.get("d_survive") is not None for c in d["cands"][:12]):
        out.append("In an elimination league a claim is worth the **survival** it "
                   "buys, not the points: six more points is worthless if you were "
                   "clearing the cut anyway. Suggested bids are that survival gain "
                   "times your remaining budget — a heuristic, but the right shape, "
                   "since budget is worth nothing after you're out.")
        out.append("")
    useful = [c for c in d["cands"] if c["gain"] > 0]
    if not useful:
        out.append("_Nothing on the wire improves your starting lineup. "
                   "Save the FAAB._")
    else:
        out.append("| | player | pos | adds | ppg | snap% | Δsnap | tgt | Δtgt | call |")
        out.append("|---:|---|---|---:|---:|---:|---:|---:|---:|---|")
        for i, c in enumerate(useful[:6]):
            call, why = weekly.claim_call(c["gain"], c.get("heat"), L.waiver_style, live)
            gain = f"**+{c['gain']:.0f}**"
            out.append(f"| {i+1} | {c['name']} | {c['pos']} | {gain} | {c['ppg']:.1f} | "
                       f"{c['snap']*100:.0f}% | {c['dsnap']*100:+.0f} | {c['tgt']:.1f} | "
                       f"{c['dtgt']:+.1f} | **{call}** — {why} |")
        out.append("")
        top = useful[0]
        call, why = weekly.claim_call(top["gain"], top.get("heat"), L.waiver_style, live)
        out.append(f"**Priority: {top['name']}.** Adds {top['gain']:.0f} points to your "
                   f"projected starting lineup — {call}, {why}.")
        bench = [c for c in d["cands"] if c["gain"] <= 0][:3]
        if bench:
            out.append("")
            out.append("Producing but *don't* improve your lineup — you're already "
                       "covered at their position, so don't spend a claim: "
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
