"""The draft board: what a player is worth to you, versus what he'll cost.

`edge` is the whole point. It is (ADP rank) - (your VBD rank):
    positive -> the room undervalues him; he'll still be there when you want him
    negative -> the room overvalues him; you'd have to reach
"""
from __future__ import annotations

from . import adp as adp_mod
from . import vbd
from .names import key


def build(projections: list[dict], league, undrafted_after: int | None = None
          ) -> tuple[list[dict], dict]:
    """Full board for one league. Returns (rows, meta).

    Edge is computed ONLY over the intersection of the two universes, with both
    sides re-ranked inside it. This matters: FFC lists ~257 players while our
    board has ~430, so comparing raw ranks across them manufactures fake edge for
    deep players purely from the size difference. (Same pool-size artifact that
    invalidated the original bias hypothesis -- see ANALYSIS.md section 3b.)
    """
    rows = vbd.build(projections, league)
    market, source, stdev = adp_mod.market_for(league, projections)

    # ESPN assigns sentinel ADPs (400+) to players nobody drafts. Those are
    # "undrafted", not bargains. Cut the market off at a plausible draft depth.
    cutoff = undrafted_after or (league.teams * (league.starter_slots + league.bench))
    market = {k: v for k, v in market.items() if 0 < v <= cutoff}

    board_keys = {key(r["name"], r["position"]) for r in rows}
    shared = board_keys & set(market)

    # Re-rank BOTH sides within the shared universe so the scales are identical.
    mkt_rank = {k: i for i, k in
                enumerate(sorted(shared, key=lambda k: market[k]), 1)}
    you_rank = {}
    for i, r in enumerate(
            sorted((r for r in rows if key(r["name"], r["position"]) in shared),
                   key=lambda r: r["vbd_rank"]), 1):
        you_rank[key(r["name"], r["position"])] = i

    for r in rows:
        k = key(r["name"], r["position"])
        r["adp"] = market.get(k)
        r["adp_rank"] = mkt_rank.get(k)
        r["your_rank"] = you_rank.get(k)
        r["edge"] = (r["adp_rank"] - r["your_rank"]) if k in shared else None
        # how far he realistically slides -- drives survival odds during the draft
        r["adp_sd"] = (adp_mod.spread_for(r["adp"], stdev.get(k))
                       if r["adp"] else None)

    meta = {
        "source": source,
        "matched": len(shared),
        "total": len(rows),
        "market_size": len(market),
        "cutoff": cutoff,
        "match_rate": round(len(shared) / len(market), 3) if market else 0.0,
    }
    return rows, meta


def values(rows, limit=15, min_adp_rank=None):
    """Players the room undervalues most (draft these later than their worth)."""
    c = [r for r in rows if r["edge"] is not None and r["vbd_rank"] <= 150]
    if min_adp_rank:
        c = [r for r in c if r["adp_rank"] <= min_adp_rank]
    return sorted(c, key=lambda r: -r["edge"])[:limit]


def reaches(rows, limit=15):
    """Players the room overvalues most (let someone else pay)."""
    c = [r for r in rows if r["edge"] is not None and r["adp_rank"] <= 120]
    return sorted(c, key=lambda r: r["edge"])[:limit]
