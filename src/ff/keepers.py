"""Keeper valuation.

Keeping a player is not free: it costs the pick you'd otherwise spend. So the
question is never "is this player good" but "is he worth MORE than whoever I'd
draft at that cost". That difference is the surplus, and it's the only thing
that should decide keepers.

Expected value at a pick comes from the market: at pick P the board has roughly
been picked clean down to ADP rank P, so the player you'd get is the one sitting
at that rank. We read his VORP off the same league board.
"""
from __future__ import annotations

import json
import pathlib

import requests

from .names import key

SLEEPER = "https://api.sleeper.app/v1"


def roster_and_costs(prev_league_id: str, owner_id: str, players_blob: dict,
                     escalation: int = 1, undrafted_round: int | None = None) -> list[dict]:
    """Last season's roster with each player's keeper cost in draft rounds.

    `escalation` is how many rounds earlier a kept player costs (house rule here
    is 1: drafted in round 5 -> keep for a round-4 pick).
    """
    rosters = requests.get(f"{SLEEPER}/league/{prev_league_id}/rosters", timeout=30).json()
    mine = next((r for r in rosters if r.get("owner_id") == owner_id), None)
    if mine is None:
        raise RuntimeError(f"no roster for owner {owner_id} in league {prev_league_id}")

    league = requests.get(f"{SLEEPER}/league/{prev_league_id}", timeout=30).json()
    picks = requests.get(f"{SLEEPER}/draft/{league['draft_id']}/picks", timeout=30).json()
    drafted = {p["player_id"]: p for p in picks}
    max_round = max((p["round"] for p in picks), default=15)

    out = []
    for pid in mine.get("players") or []:
        pl = players_blob.get(pid) or {}
        name = f"{pl.get('first_name','')} {pl.get('last_name','')}".strip()
        pos = pl.get("position")
        pick = drafted.get(pid)
        if pick:
            drafted_round = pick["round"]
            cost = max(1, drafted_round - escalation)
        else:
            drafted_round = None
            cost = undrafted_round or max_round      # house rule; confirm this
        out.append({
            "player_id": pid,
            "name": name,
            "position": pos,
            "drafted_round": drafted_round,
            "cost_round": cost,
            "was_undrafted": pick is None,
        })
    return out


def value(candidates: list[dict], rows: list[dict], league,
          draft_slot: int | None = None) -> list[dict]:
    """Attach 2026 value and surplus-over-replacement-pick to each candidate."""
    board = {key(r["name"], r["position"]): r for r in rows}

    # market rank -> vorp, for "what would this pick have gotten me"
    priced = sorted((r for r in rows if r.get("adp_rank")), key=lambda r: r["adp_rank"])
    by_market = {r["adp_rank"]: r for r in priced}
    deepest = max(by_market) if by_market else 0

    def pick_number(rd: int) -> int:
        # Without a known draft slot, assume the middle of the round.
        slot = draft_slot or (league.teams + 1) / 2
        return int(round((rd - 1) * league.teams + slot))

    def expected_at(rd: int):
        p = pick_number(rd)
        for q in range(p, deepest + 1):          # next player actually on the board
            if q in by_market:
                return by_market[q], p
        return None, p

    out = []
    for c in candidates:
        row = board.get(key(c["name"], c["position"]))
        alt, pno = expected_at(c["cost_round"])
        rec = dict(c)
        rec["pick_number"] = pno
        if row is None:
            rec.update(vorp=None, surplus=None, alt=None, alt_vorp=None,
                       note="no 2026 projection")
        else:
            # Floor the alternative at replacement level (VORP 0). A pick whose
            # expected product is BELOW replacement isn't really what you give
            # up -- you'd just drop him and stream someone off waivers. Without
            # this floor, deep-round keepers score well merely because the
            # counterfactual is bad, which made a -50 VORP QB look like +52.
            alt_vorp = max(alt["vorp"], 0.0) if alt else 0.0
            rec.update(
                vorp=row["vorp"], points=row["points"], vbd_rank=row["vbd_rank"],
                pos_rank=row["pos_rank"], market_rank=row.get("adp_rank"),
                alt=alt["name"] if alt else None, alt_vorp=alt_vorp,
                surplus=round(row["vorp"] - alt_vorp, 1), note="",
            )
        out.append(rec)

    out.sort(key=lambda r: (r["surplus"] is None, -(r["surplus"] or 0)))
    return out
