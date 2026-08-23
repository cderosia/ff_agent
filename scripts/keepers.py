#!/usr/bin/env python3
"""Keeper recommendations for any league configured with keeper settings.

    python3 scripts/keepers.py
    python3 scripts/keepers.py --league freinds-keeper
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

from ff import board, keepers            # noqa: E402
from ff.leagues import configured_slot, load_all   # noqa: E402
from ff.projections import fetch         # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "boards"
PLAYERS_CACHE = ROOT / "data" / "raw" / "sleeper_players.json"


def sleeper_players() -> dict:
    """The 12k-player Sleeper blob. Big, rarely changes -- cache it hard."""
    PLAYERS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    if PLAYERS_CACHE.exists():
        return json.loads(PLAYERS_CACHE.read_text())
    d = requests.get("https://api.sleeper.app/v1/players/nfl", timeout=120).json()
    PLAYERS_CACHE.write_text(json.dumps(d))
    return d



def rd_pick(overall, teams) -> str:
    """An overall pick number as round.pick -- '3.04 (24)'."""
    if not overall:
        return "—"
    n = int(round(overall))
    rd = (n - 1) // teams + 1
    within = n - (rd - 1) * teams
    return f"{rd}.{within:02d} ({n})"


def render(league, res, cfg, meta) -> str:
    k = cfg["keepers"]
    positive = [r for r in res if (r.get("surplus") or 0) > 0][: k["max"]]
    L = [f"# {league.name} — keepers", ""]
    L.append(f"*generated {datetime.now():%Y-%m-%d %H:%M} · market {meta['source']}*")
    L.append("")
    L.append(f"Keep up to **{k['max']}**; a kept player costs a pick "
             f"**{k['escalation']} round earlier** than he was drafted.")
    L.append("")
    L.append("**Surplus** is his 2026 VORP minus the VORP you'd expect from the pick "
             "he costs. The alternative is floored at replacement level, because a pick "
             "whose expected product is below replacement isn't a real loss — you'd drop "
             "him and stream someone off waivers.")
    L.append("")
    L.append("## Recommendation")
    L.append("")
    if positive:
        for r in positive:
            L.append(f"- **Keep {r['name']}** ({r['pos_rank']}) for a round-{r['cost_round']} "
                     f"pick — surplus **{r['surplus']:+.0f}** "
                     f"(vs. ~{r['alt']} at pick {r['pick_number']})")
        L.append("")
        L.append(f"Total surplus **{sum(r['surplus'] for r in positive):+.0f} VORP** "
                 f"across {len(positive)} keepers, spending "
                 f"{', '.join('rd'+str(r['cost_round']) for r in positive)}.")
    else:
        L.append("- No player is worth his keeper cost. Draft clean.")
    if len(positive) < k["max"]:
        L.append("")
        L.append(f"> Only {len(positive)} of a possible {k['max']} keepers clear zero. "
                 f"Do **not** fill the remaining slots — keeping a negative-surplus "
                 f"player is strictly worse than using the pick.")
    L.append("")
    L.append("## Full evaluation")
    L.append("")
    L.append("Columns: **costs you** is the pick you surrender to keep him. "
             "**our board** is where he ranks on this league's own board, as a "
             "round.pick — our answer to ADP. **market** is where the room "
             "actually drafts him. Keep him when what you pay is later than "
             "where he belongs.")
    L.append("")
    L.append("| | player | pos | costs you | our board | market | instead you'd get "
             "| vorp | surplus |")
    L.append("|:--|---|---|---|---|---|---|---:|---:|")
    for r in res:
        if r.get("surplus") is None:
            cost_s = (f"rd{r['cost_round']} ({r['pick_number']})"
                      if r.get('cost_round') and r.get('pick_number')
                      else (f"rd{r['cost_round']}" if r.get('cost_round') else "—"))
            L.append(f"| | {r['name']} | {r['position']} | {cost_s} | — | — | "
                     f"_{r['note']}_ | — | — |")
            continue
        mark = "**KEEP**" if r in positive else ""
        alt = f"{r['alt']} ({max(r['alt_vorp'],0):.0f})" if r["alt"] else "—"
        cost = f"rd{r['cost_round']} · {rd_pick(r['pick_number'], league.teams)}"
        ours = rd_pick(r.get("vbd_rank"), league.teams)
        mkt = rd_pick(r.get("adp"), league.teams)
        L.append(f"| {mark} | {r['name']} | {r['pos_rank']} | {cost} | {ours} | "
                 f"{mkt} | {alt} | {r['vorp']:.0f} | {r['surplus']:+.0f} |")
    L.append("")
    L.append("## Rule assumptions")
    L.append("")
    L.append("These were not readable from the API and are set in `leagues.yaml`. "
             "**Confirm them with your commissioner** — they change the answer:")
    L.append("")
    r1 = k.get("round_one_keepable", True)
    L.append(f"- Round-1 picks are **{'keepable at a round-1 cost' if r1 else 'NOT keepable'}**.")
    L.append(f"- Undrafted / waiver adds cost a **round-{k['undrafted_round']}** pick.")
    L.append(f"- A player acquired by trade or waiver keeps the round the "
             f"**original drafter** spent: `{k['acquired_keeps_original_round']}`.")
    L.append("")
    L.append("Escalation compounds across years, which isn't modelled here — a cheap "
             "keeper you intend to hold for several seasons is worth slightly more "
             "than its one-year surplus suggests.")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league")
    args = ap.parse_args()

    cfgs = {c["name"]: c for c in
            yaml.safe_load((ROOT / "leagues" / "leagues.yaml").read_text())["leagues"]}
    proj = fetch()
    leagues, _ = load_all()
    blob = sleeper_players()
    OUT.mkdir(exist_ok=True)

    found = False
    for L in leagues:
        cfg = cfgs.get(L.name, {})
        if not cfg.get("keepers") or not cfg.get("owner_id"):
            continue
        if args.league and L.name != args.league:
            continue
        found = True
        k = cfg["keepers"]
        prev = L.raw.get("previous_league_id")
        if not prev:
            print(f"  {L.name}: no previous_league_id — can't read last year's draft")
            continue

        rows, meta = board.build(proj, L)
        cands = keepers.roster_and_costs(
            prev, cfg["owner_id"], blob,
            escalation=k.get("escalation", 1),
            undrafted_round=k.get("undrafted_round"),
            round_one_keepable=k.get("round_one_keepable", True),
        )
        cands = [c for c in cands if c["position"] not in ("K", "DEF", "DST")]
        res = keepers.value(cands, rows, L,
                            draft_slot=configured_slot(cfg))

        path = OUT / f"{L.name}-keepers.md"
        path.write_text(render(L, res, cfg, meta))
        pos = [r for r in res if (r.get("surplus") or 0) > 0][: k["max"]]
        print(f"  {L.name}: {len(pos)}/{k['max']} keepers worth it "
              f"(+{sum(r['surplus'] for r in pos):.0f} VORP) -> {path.relative_to(ROOT)}")

    if not found:
        print("no leagues have `keepers:` and `owner_id:` configured")


if __name__ == "__main__":
    main()
