"""Trade search and evaluation.

The naive way to score a trade is to sum VORP on each side. That's wrong,
because your bench scores you nothing. What matters is the change in your
projected STARTING lineup -- which means:

  * consolidation is real. Two RB2s for one RB1 can be a clear win at equal
    VORP totals, because only one of the two was ever in your lineup, and the
    roster spot you free up gets refilled off waivers.
  * the same player is worth different amounts to different teams. A third
    good RB is nearly worthless to a team that starts two, and valuable to a
    team starting a replacement-level one. That asymmetry is exactly what
    makes mutually-positive trades possible.

So every package is scored twice: once for you, once for them. And separately
we compute OPTICS -- the raw VORP differential, which is what the other manager
will actually eyeball. A trade that helps you both but looks lopsided on a
trade-value chart gets rejected. The best trades to propose are the ones where
you give up MORE raw value than you get and still gain lineup points.
"""
from __future__ import annotations

from itertools import combinations

import requests

from .draft import lineup_value
from .names import key
from .stats import SLOT_ELIGIBILITY


def league_rosters(league_id: str, blob: dict, by_key: dict,
                   me: str | None) -> tuple[list[dict], list[dict]]:
    """Every team's roster as board rows. Returns (my_roster, [other teams])."""
    rs = requests.get(f"https://api.sleeper.app/v1/league/{league_id}/rosters",
                      timeout=30).json()
    users = requests.get(f"https://api.sleeper.app/v1/league/{league_id}/users",
                         timeout=30).json()
    names = {u["user_id"]: (u.get("display_name") or u["user_id"]) for u in users}

    mine, others = [], []
    for r in rs:
        players = []
        for pid in r.get("players") or []:
            p = blob.get(pid) or {}
            row = by_key.get(key(f"{p.get('first_name','')} {p.get('last_name','')}".strip(),
                                 p.get("position")))
            if row:
                players.append(row)
        entry = {"roster_id": r["roster_id"], "owner": r.get("owner_id"),
                 "name": names.get(r.get("owner_id"), f"team {r['roster_id']}"),
                 "players": players}
        if me and r.get("owner_id") == me:
            mine = players
        else:
            others.append(entry)
    return mine, others


def surplus(roster: list[dict], league) -> dict:
    """Per position: how much value sits on the bench behind a filled slot.

    High surplus = tradeable depth. Negative = a hole worth trading for.
    """
    repl = league.replacement
    out = {}
    for pos in ("QB", "RB", "WR", "TE"):
        starts = sum(c for s, c in league.starters.items()
                     if pos in SLOT_ELIGIBILITY.get(s, {s}))
        if not starts:
            continue
        at_pos = sorted((p for p in roster if p["position"] == pos),
                        key=lambda p: -p["points"])
        base = repl.get(pos, 0.0)
        starters_have = at_pos[:starts]
        # value above replacement sitting in reserve
        bench_val = sum(max(0.0, p["points"] - base) for p in at_pos[starts:])
        # how far below replacement your worst notional starter is
        hole = sum(max(0.0, base - p["points"]) for p in starters_have)
        if len(starters_have) < starts:
            hole += (starts - len(starters_have)) * 0.0
        out[pos] = {"depth": round(bench_val, 1), "hole": round(hole, 1),
                    "n": len(at_pos), "starts": starts}
    return out


def evaluate(my_roster, their_roster, give, get, league) -> dict:
    """Score one package from both sides."""
    repl = league.replacement
    gk = {id(p) for p in give}
    tk = {id(p) for p in get}

    mine_after = [p for p in my_roster if id(p) not in gk] + list(get)
    theirs_after = [p for p in their_roster if id(p) not in tk] + list(give)

    my_delta = lineup_value(mine_after, league, repl) - lineup_value(my_roster, league, repl)
    their_delta = (lineup_value(theirs_after, league, repl)
                   - lineup_value(their_roster, league, repl))

    # what a trade-value chart would say -- positive means you gave up more
    optics = sum(p["vorp"] for p in give) - sum(p["vorp"] for p in get)

    return {"give": list(give), "get": list(get),
            "my_delta": round(my_delta, 1), "their_delta": round(their_delta, 1),
            "optics": round(optics, 1)}


def find(my_roster, others, league, min_my_gain=8.0, min_their_gain=3.0,
         max_side=2, pool=14, limit=12, rank="mine") -> list[dict]:
    """Search packages that improve BOTH teams' starting lineups.

    `pool` caps how many players per side enter the search -- the combinatorics
    explode otherwise and deep bench players never move the needle anyway.
    """
    mine = sorted(my_roster, key=lambda p: -p["points"])[:pool]
    found = []

    for team in others:
        theirs = sorted(team["players"], key=lambda p: -p["points"])[:pool]
        if not theirs:
            continue
        for gn in range(1, max_side + 1):
            for gt in range(1, max_side + 1):
                # only consider consolidation or even swaps, not fragmentation
                if gn > 1 and gt > 1:
                    continue
                for give in combinations(mine, gn):
                    for get in combinations(theirs, gt):
                        r = evaluate(my_roster, team["players"], give, get, league)
                        if (r["my_delta"] >= min_my_gain
                                and r["their_delta"] >= min_their_gain):
                            r["team"] = team["name"]
                            found.append(r)

    found = [r for r in found
             if _minimal(r, my_roster, _team_of(others, r["team"]), league, min_their_gain)]

    # Two ways to rank, and they give very different lists:
    #   "mine" -- most points for you. Maximising your gain subject to a small
    #             gain for them naturally produces lopsided deals, so most of
    #             these get rejected. Useful as an upper bound on what to ask.
    #   "fair" -- maximise the weaker side of the deal and keep the optics
    #             defensible. Fewer points for you, far more likely to be
    #             accepted, which is the only kind that actually happens.
    if rank == "fair":
        found = [r for r in found if r["optics"] > -15]
        found.sort(key=lambda r: (-min(r["my_delta"], r["their_delta"]), -r["optics"]))
    else:
        found.sort(key=lambda r: (-r["my_delta"], -r["optics"]))

    # de-dup on what you give up: one best idea per asset you're shopping
    seen, out = set(), []
    for r in found:
        sig = (r["team"], tuple(sorted(p["name"] for p in r["give"])))
        if sig in seen:
            continue
        seen.add(sig)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _team_of(others, name):
    return next((t["players"] for t in others if t["name"] == name), [])


def _minimal(r, my_roster, their_roster, league, min_their_gain) -> bool:
    """Drop packages containing dead weight.

    A received player who doesn't raise your gain is filler, and a given player
    you could keep while the deal still works for them is a giveaway. Without
    this, the search returns the same trade a dozen times with different
    throw-ins, all showing an identical gain.
    """
    if len(r["get"]) > 1:
        for drop in r["get"]:
            smaller = [p for p in r["get"] if p is not drop]
            alt = evaluate(my_roster, their_roster, r["give"], smaller, league)
            if alt["my_delta"] >= r["my_delta"] - 0.01:
                return False               # that player added nothing
    if len(r["give"]) > 1:
        for drop in r["give"]:
            smaller = [p for p in r["give"] if p is not drop]
            alt = evaluate(my_roster, their_roster, smaller, r["get"], league)
            if (alt["my_delta"] >= r["my_delta"] - 0.01
                    and alt["their_delta"] >= min_their_gain):
                return False               # you didn't need to include him
    return True


def review(offer_give, offer_get, my_roster, their_roster, league) -> dict:
    """Evaluate an incoming offer. `offer_give` is what they want FROM you."""
    r = evaluate(my_roster, their_roster, offer_give, offer_get, league)
    r["verdict"] = verdict(r)
    r["accept"] = r["my_delta"] > 0
    return r


def verdict(r: dict) -> str:
    """One line on whether to send/accept, and how it will be received."""
    if r["my_delta"] <= 0:
        return "Decline — costs you starting points."
    if r["their_delta"] <= 0:
        return "They have no reason to accept."
    if r["optics"] > 5:
        return "Send it — you give up more on paper than you get, so it reads generous."
    if r["optics"] < -15:
        return "Likely rejected — looks lopsided in your favour on a value chart."
    return "Reasonable both ways."
