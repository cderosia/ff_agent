"""One or two sentences on why a player is the pick.

The board ranks by numbers that took a lot of machinery to compute -- marginal
lineup value, expected value of your next pick, expected starts, live edge --
and on draft day none of that is legible in the ninety seconds you have. A
column of figures tells you WHICH player; it doesn't tell you WHY, and without
the why you can't tell a genuine call from a bug, or overrule it when you know
something the projections don't.

So: read the same numbers the ranking used, find the two that actually drive
this pick, and say them in English.
"""
from __future__ import annotations

from . import starts as starts_mod
from .stats import SLOT_ELIGIBILITY


def _open_slot(row, league, roster) -> str | None:
    """The starting slot this player would walk into, if any."""
    from .draft import _assign
    filled = _assign(roster, league)
    for slot, cnt in league.starters.items():
        if slot in ("K", "DST"):
            continue
        if row["position"] not in SLOT_ELIGIBILITY.get(slot, {slot}):
            continue
        if len(filled.get(slot, [])) < cnt:
            return slot
    return None


def _bye_note(row, roster, byes) -> str | None:
    """Whether he covers or collides with the byes you already carry."""
    mine = starts_mod.bye_for(row.get("team"), byes)
    if mine is None:
        return None
    same = [p for p in roster
            if p["position"] == row["position"]
            and starts_mod.bye_for(p.get("team"), byes) == mine]
    if len(same) >= 2:
        return f"but that's {len(same) + 1} {row['position']}s on the week {mine} bye"
    return None


def reasons(row, league, roster, *, next_pick=None, following_pick=None,
            tier_of=None, byes=None, run=None) -> str:
    """A short, concrete case for taking this player. '' if nothing stands out."""
    byes = byes if byes is not None else starts_mod.bye_by_team()
    bits = []

    # 1. Does he start for you right now? That outranks everything else.
    slot = _open_slot(row, league, roster)
    gain = row.get("marginal") or 0
    if slot and gain > 0:
        bits.append(f"steps straight into your empty {slot} for +{gain:.0f} "
                    f"points over what you'd start there now")
    elif gain > 0:
        bits.append(f"upgrades your lineup by {gain:.0f} points")

    # 2. Will he be there next time? This is the whole reason the order isn't
    #    just "best player available".
    gone = row.get("gone_pct")
    if gone is not None and following_pick:
        if gone >= 70:
            bits.append(f"and he will not last -- {gone}% gone by pick "
                        f"{following_pick}")
        elif gone <= 25 and bits:
            bits.append(f"though at {gone}% gone he'd likely still be there at "
                        f"{following_pick}, so this is not urgent")

    # 3. Tier cliff: the last man above a drop is worth reaching for.
    if tier_of:
        t = tier_of.get(row["name"])
        if t and t.get("last_of_tier") and t.get("drop"):
            bits.append(f"he's the last of tier {t['tier']} at {row['position']}"
                        f" -- the next one down is {t['drop']:.0f} points worse")

    # 4. Bench case, once the lineup is full and marginal value is 0 for all.
    if not slot and gain <= 0:
        xs = row.get("exp_starts")
        bv = row.get("bench_val")
        if xs and bv and bv > 0:
            bits.append(f"projects to start about {xs:.0f} weeks for you, worth "
                        f"~{bv:.0f} points over a waiver streamer")
        elif bv is not None and bv <= 0:
            bits.append("depth only -- he doesn't beat what you could stream")

    # 5. Price. Edge is live: his rank in what's left vs the market's.
    edge = row.get("edge")
    if edge and edge >= 12:
        bits.append(f"and the room still has him {edge} spots later than you do")

    # 6. Caveats last, so they qualify rather than lead.
    b = _bye_note(row, roster, byes)
    if b:
        bits.append(b)
    if (row.get("rel_spread") or 0) > 0.2:
        bits.append("sources disagree on him, so treat the projection as soft")
    if run and run.get("pos") == row["position"]:
        bits.append(f"and a {run['pos']} run is on -- {run['n']} of the last "
                    f"{run['of']} picks")

    if not bits:
        return "best value left on the board for your roster."
    out = bits[0]
    for b in bits[1:3]:
        out += (" " if b.startswith(("and ", "but ", "though ")) else "; ") + b
    return out[0].upper() + out[1:] + "."
