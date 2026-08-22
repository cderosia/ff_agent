"""Live draft state and roster-aware recommendations.

Two questions during a draft, and they have different answers:

  "best value"    -> highest VORP still on the board, ignoring your roster
  "best for you"  -> how much a player improves YOUR starting lineup

The second is the one that changes as you draft. We compute it as marginal
lineup value: fill every unfilled starting slot with a replacement-level player,
then ask how much adding this player raises the total. That equals his VORP while
the slot is empty and collapses toward zero once you've filled it -- which is the
behaviour you want, without hand-tuned positional weights.

There is a third question the first two can't answer: "best for you RIGHT NOW",
i.e. who won't be there later. Marginal value alone is greedy -- it will happily
spend a pick on a position that would still be sitting there two rounds from now
while the last player above a cliff goes to someone else. `recommend()` prices
that in by scoring a pick as marginal value now plus the expected value of your
NEXT pick given that choice (see `_expected_best`), using ADP and its spread to
work out who survives.
"""
from __future__ import annotations

import collections
import math

import requests

from . import vbd
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


def roster_max(league) -> dict:
    """Most of each position worth carrying on one roster.

    From roughly round 6 on, every available player sits at or below
    replacement, so `marginal` and `plan` are 0.0 for the entire board and the
    ranking falls through to a tiebreak. Any tiebreak that slightly favours one
    position then picks that position every remaining round -- which is how a
    board recommends nine tight ends. Roster construction is the backstop: you
    start one TE, so a third is never the pick no matter what the maths says.
    """
    dedicated, flex = {}, set()
    for slot, cnt in league.starters.items():
        elig = SLOT_ELIGIBILITY.get(slot, {slot})
        if len(elig) == 1:
            dedicated[next(iter(elig))] = dedicated.get(next(iter(elig)), 0) + cnt
        else:
            flex |= elig
    out = {}
    for pos in ("QB", "RB", "WR", "TE"):
        base = dedicated.get(pos, 0)
        # Only RB/WR realistically absorb flex slots and injury depth.
        if pos in ("RB", "WR"):
            out[pos] = base + (1 if pos in flex else 0) + 2
        else:
            out[pos] = base + 1
    return out


def own_gap(player: dict, league, roster: list[dict], replacement: dict) -> float:
    """Points versus the man you'd actually bench to play him.

    `lineup_value` can only ever return a gain of >= 0, so every player who
    doesn't crack your lineup ties at exactly 0 and the ordering falls through
    to league-wide VORP. That badly over-rates a 3rd TE: TE has a low
    replacement level, so TE13 shows positive VORP while the WR who is one
    injury from your flex shows negative -- even when you already roster TE2.

    This measures the gap against YOUR incumbent at the slot he'd compete for,
    so it stays negative for the 3rd TE and ranks bench picks by how close they
    are to actually starting for you.
    """
    filled = _assign(roster, league)
    best = None
    for slot, count in league.starters.items():
        elig = SLOT_ELIGIBILITY.get(slot, {slot})
        if player["position"] not in elig:
            continue
        got = filled.get(slot, [])
        if len(got) < count:
            base = max((replacement.get(p, 0.0) for p in elig), default=0.0)
        else:
            base = min(p["points"] for p in got)   # the man he'd displace
        gap = player["points"] - base
        best = gap if best is None else max(best, gap)
    return best if best is not None else player["vorp"]


def survival(adp: float | None, sd: float | None, pick: int) -> float:
    """P(this player is still on the board when pick `pick` comes around).

    Logistic on how far `pick` sits past his ADP, scaled by how much he actually
    slides (`sd`). A player with no market price is one nobody is drafting, so he
    is assumed available.
    """
    if not adp:
        return 1.0
    s = max(1.0, float(sd or 1.0))
    # 1.7/sd makes the logistic track a normal CDF closely enough for this
    z = max(-30.0, min(30.0, 1.7 * (adp - pick) / s))
    return 1.0 / (1.0 + math.exp(-z))


def _gains(pool: list[dict], league, roster: list[dict], repl: dict,
           skip: str | None = None) -> list[tuple[float, dict]]:
    """Marginal lineup value of each pool player, given `roster`."""
    base = lineup_value(roster, league, repl)
    out = []
    for r in pool:
        if skip is not None and key(r["name"], r["position"]) == skip:
            continue
        out.append((lineup_value(roster + [r], league, repl) - base, r))
    out.sort(key=lambda t: (-t[0], -own_gap(t[1], league, roster, repl)))
    return out


def _expected_best(pool: list[dict], league, roster: list[dict], repl: dict,
                   pick: int, skip: str | None = None) -> float:
    """E[best marginal value still available at `pick`], given `roster`.

    Walk candidates best-first: you get the first one that survives. Treating
    survivals as independent, that is sum(gain_i * P(i survives) * P(none better
    survived)) -- the expected value of your next pick, which is exactly what a
    "should I wait?" question is asking about.
    """
    exp, none_yet = 0.0, 1.0
    for gain, r in _gains(pool, league, roster, repl, skip):
        p = survival(r.get("adp"), r.get("adp_sd"), pick)
        exp += none_yet * p * gain
        none_yet *= (1.0 - p)
        if none_yet < 1e-3:
            break
    return exp


def recommend(rows: list[dict], league, taken: set, my_players: list[dict],
              limit: int = 12, next_pick: int | None = None,
              following_pick: int | None = None, depth: int = 40,
              pool_size: int = 100) -> list[dict]:
    """Rank available players by what they're worth to YOUR lineup.

    With `following_pick`, this stops being greedy. Taking a player is scored as
    marginal value now PLUS the expected value of your next pick given that
    choice, so the cost of waiting is priced into the ranking instead of sitting
    in a box beside it: grabbing the last player above a cliff leaves you a
    healthy next pick, while taking the position that will still be there leaves
    you the scraps. Without pick numbers we can't know what survives, so it falls
    back to the greedy ordering.
    """
    avail = [r for r in rows if key(r["name"], r["position"]) not in taken]
    # Replacement has to track remaining supply or the board flatlines from
    # round 7 on -- see vbd.dynamic_replacement.
    taken_by_pos = collections.Counter(
        r["position"] for r in rows if key(r["name"], r["position"]) in taken)
    repl = vbd.dynamic_replacement(avail, league, taken_by_pos)

    # Two filters, applied in order, each with its own fallback. Both matter:
    # without them the board recommends nine tight ends (see roster_max).
    #
    #   1. rosterable depth -- drop the unpickable tail of each position, so
    #      deep TEs and backup QBs stop crowding out real RB/WR depth.
    #   2. roster construction -- drop positions THIS roster is already full
    #      at, which is the only thing that still bites once every marginal
    #      value has flattened to 0.
    pos_depth = vbd.rosterable_depth(league)
    held = collections.Counter(p["position"] for p in my_players)
    caps = roster_max(league)

    def in_depth(r):
        return (r.get("pos_rank_n") is None
                or r["pos_rank_n"] <= pos_depth.get(r["position"], 10**6))

    def needed(r):
        return held.get(r["position"], 0) < caps.get(r["position"], 99)

    pool_rows = [r for r in avail if in_depth(r) and needed(r)]
    if len(pool_rows) < limit:
        # Relax depth before construction: a 4th RB beyond the nominal depth
        # is a real pick, a 5th TE is not. Only if that still starves the
        # board do we hand back the full list.
        pool_rows = [r for r in avail if needed(r)]
    if len(pool_rows) < limit:
        seen = {id(r) for r in pool_rows}
        flex_first = {"RB": 0, "WR": 0, "TE": 1, "QB": 2}
        extra = sorted((r for r in avail if id(r) not in seen),
                       key=lambda r: (flex_first.get(r["position"], 3), -r["vorp"]))
        pool_rows = pool_rows + extra[: limit - len(pool_rows)]
    avail = pool_rows
    ranked = _gains(avail, league, my_players, repl)

    # "Will he be back?" is a question about your NEXT turn, not this one. Scoring
    # it at `next_pick` describes whether he'd have survived to the pick you are
    # already making -- which is answered by the fact that he's sitting there.
    risk_at = following_pick or next_pick
    out = []
    for gain, r in ranked:
        row = {**r, "marginal": round(gain, 1),
               "own_gap": round(own_gap(r, league, my_players, repl), 1)}
        if risk_at:
            row["gone_pct"] = round(
                100 * (1 - survival(r.get("adp"), r.get("adp_sd"), risk_at)))
        out.append(row)

    if not following_pick:
        # Primary sort on marginal value; VORP breaks ties and keeps upside
        # visible once your starters are full and marginal values collapse to 0.
        out.sort(key=lambda r: (-r["marginal"], -r["own_gap"]))
        return out[:limit]

    pool = [r for _, r in ranked[:pool_size]]
    for row in out[:depth]:
        k = key(row["name"], row["position"])
        after = my_players + [row]
        nxt = _expected_best(pool, league, after, repl, following_pick, skip=k)
        row["next_best"] = round(nxt, 1)
        row["plan"] = round(row["marginal"] + nxt, 1)
    # Anything past `depth` was never in contention; keep it ordered behind.
    for row in out[depth:]:
        row["next_best"] = None
        row["plan"] = row["marginal"] - 1e6
    out.sort(key=lambda r: (-r["plan"], -r["own_gap"]))
    for row in out[depth:]:
        row["plan"] = None
    return out[:limit]


def roster_gaps(my_players: list[dict], league) -> dict:
    """Unfilled starting slots."""
    filled = _assign(my_players, league)
    return {slot: cnt - len(filled.get(slot, []))
            for slot, cnt in league.starters.items()
            if cnt - len(filled.get(slot, [])) > 0}
