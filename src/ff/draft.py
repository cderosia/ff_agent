"""Live draft state and roster-aware recommendations.

Two questions during a draft, and they have different answers:

  "best value"    -> highest VORP still on the board, ignoring your roster
  "best for you"  -> how much a player improves YOUR starting lineup

The second is the one that changes as you draft. We compute it as marginal
lineup value: fill every unfilled starting slot with a replacement-level player,
then ask how much adding this player raises the total. That equals his VORP while
the slot is empty and collapses toward zero once you've filled it -- which is the
behaviour you want, without hand-tuned positional weights.
"""
from __future__ import annotations

import requests

from .names import key
from .stats import SLOT_ELIGIBILITY

SLEEPER = "https://api.sleeper.app/v1"
ESPN = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
UA = {"User-Agent": "Mozilla/5.0"}


# ---------------------------------------------------------------------------
# Pick feeds
# ---------------------------------------------------------------------------
def sleeper_picks(draft_id: str) -> list[dict]:
    r = requests.get(f"{SLEEPER}/draft/{draft_id}/picks", timeout=20)
    r.raise_for_status()
    out = []
    for p in r.json():
        m = p.get("metadata") or {}
        name = f"{m.get('first_name','')} {m.get('last_name','')}".strip()
        out.append({
            "pick_no": p["pick_no"], "round": p["round"],
            "name": name, "position": m.get("position"),
            "by": p.get("picked_by"), "slot": p.get("draft_slot"),
        })
    return sorted(out, key=lambda x: x["pick_no"])


def espn_picks(league_id: str, cookies: dict, id_to_player: dict) -> list[dict]:
    url = (f"{ESPN}/seasons/2026/segments/0/leagues/{league_id}?view=mDraftDetail")
    r = requests.get(url, headers=UA, cookies=cookies, timeout=20)
    r.raise_for_status()
    detail = r.json().get("draftDetail") or {}
    out = []
    for p in detail.get("picks") or []:
        pid = p.get("playerId", -1)
        if pid is None or pid <= 0:
            continue                       # slot not yet used
        pl = id_to_player.get(pid) or {}
        out.append({
            "pick_no": p["overallPickNumber"], "round": p["roundId"],
            "name": pl.get("name", f"espn:{pid}"), "position": pl.get("position"),
            "by": p.get("teamId"), "slot": p.get("roundPickNumber"),
        })
    return sorted(out, key=lambda x: x["pick_no"])


def espn_draft_order(league_id: str, cookies: dict) -> dict:
    """{round_pick_number: teamId} for round 1 -- i.e. the draft order."""
    url = f"{ESPN}/seasons/2026/segments/0/leagues/{league_id}?view=mDraftDetail"
    r = requests.get(url, headers=UA, cookies=cookies, timeout=20)
    detail = r.json().get("draftDetail") or {}
    return {p["roundPickNumber"]: p["teamId"]
            for p in (detail.get("picks") or []) if p["roundId"] == 1}


# ---------------------------------------------------------------------------
# Lineup value
# ---------------------------------------------------------------------------
def _assign(players: list[dict], league) -> dict:
    """Greedily fill starting slots with the best eligible players."""
    open_slots = dict(league.starters)
    dedicated = [s for s in open_slots if len(SLOT_ELIGIBILITY.get(s, {s})) == 1]
    flexes = [s for s in open_slots if len(SLOT_ELIGIBILITY.get(s, {s})) > 1]
    filled = {s: [] for s in open_slots}
    for p in sorted(players, key=lambda x: -x["points"]):
        for slot in dedicated + flexes:
            if open_slots.get(slot, 0) > 0 and p["position"] in SLOT_ELIGIBILITY.get(slot, {slot}):
                open_slots[slot] -= 1
                filled[slot].append(p)
                break
    return filled


def lineup_value(players: list[dict], league, replacement: dict) -> float:
    """Projected starting-lineup points, with empty slots at replacement level."""
    filled = _assign(players, league)
    total = 0.0
    for slot, count in league.starters.items():
        got = filled.get(slot, [])
        total += sum(p["points"] for p in got)
        empty = count - len(got)
        if empty > 0:
            elig = SLOT_ELIGIBILITY.get(slot, {slot})
            base = max((replacement.get(p, 0.0) for p in elig), default=0.0)
            total += empty * base
    return total


def recommend(rows: list[dict], league, taken: set, my_players: list[dict],
              limit: int = 12) -> list[dict]:
    """Rank available players by marginal value to YOUR lineup."""
    repl = league.replacement
    base = lineup_value(my_players, league, repl)
    avail = [r for r in rows if key(r["name"], r["position"]) not in taken]

    out = []
    for r in avail:
        gain = lineup_value(my_players + [r], league, repl) - base
        out.append({**r, "marginal": round(gain, 1)})
    # Primary sort on marginal value; VORP breaks ties and keeps upside visible
    # once your starters are full and every marginal value collapses to zero.
    out.sort(key=lambda r: (-r["marginal"], -r["vorp"]))
    return out[:limit]


def roster_gaps(my_players: list[dict], league) -> dict:
    """Unfilled starting slots."""
    filled = _assign(my_players, league)
    return {slot: cnt - len(filled.get(slot, []))
            for slot, cnt in league.starters.items()
            if cnt - len(filled.get(slot, [])) > 0}
