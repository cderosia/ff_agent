"""Value-based drafting: replacement levels derived from real starter demand.

Replacement level is the whole ballgame. It is NOT a fixed rank like "RB24" --
it depends on team count and on how flex slots get consumed, both of which vary
a lot across leagues. We derive it by simulating league-wide starter demand.
"""
from __future__ import annotations

from .stats import SLOT_ELIGIBILITY, score


def replacement_levels(players: list[dict], league) -> tuple[dict, dict]:
    """Simulate filling every team's starting lineup, best-player-first.

    Returns ({position: replacement_points}, {position: starters_consumed}).

    Walking players in descending points and assigning each to a dedicated slot
    if one is open, else to any flex it qualifies for, reproduces how a league's
    starter demand actually distributes -- which is what sets replacement level.
    """
    # league-wide slot demand
    open_slots = {slot: cnt * league.teams for slot, cnt in league.starters.items()}
    dedicated = [s for s in open_slots if len(SLOT_ELIGIBILITY.get(s, {s})) == 1]
    flexes = [s for s in open_slots if len(SLOT_ELIGIBILITY.get(s, {s})) > 1]

    used = {}
    for p in sorted(players, key=lambda x: -x["points"]):
        pos = p["position"]
        placed = False
        for slot in dedicated:
            if open_slots[slot] > 0 and pos in SLOT_ELIGIBILITY.get(slot, {slot}):
                open_slots[slot] -= 1
                placed = True
                break
        if not placed:
            for slot in flexes:
                if open_slots[slot] > 0 and pos in SLOT_ELIGIBILITY.get(slot, {slot}):
                    open_slots[slot] -= 1
                    placed = True
                    break
        if placed:
            used[pos] = used.get(pos, 0) + 1
        if sum(open_slots.values()) == 0:
            break

    repl = {}
    by_pos = {}
    for p in players:
        by_pos.setdefault(p["position"], []).append(p["points"])
    for pos, pts in by_pos.items():
        pts.sort(reverse=True)
        n = used.get(pos, 0)
        # replacement = the best player who does NOT start anywhere
        repl[pos] = pts[n] if n < len(pts) else (pts[-1] if pts else 0.0)
    return repl, used


# K and DST are deliberately excluded from the board:
#   - ESPN publishes no 2026 DST projections in this feed (verified: 0 rows).
#   - Sleeper scores field goals by finer distance buckets than ESPN reports,
#     so any K mapping would be lossy guesswork.
# Neither position is flex-eligible, so excluding them has NO effect on the
# replacement levels of QB/RB/WR/TE. They are last-round picks; draft them off
# the platform's own list.
SKIP_POSITIONS = {"K", "DST"}


def build(projections: list[dict], league) -> list[dict]:
    """Score projections under one league's rules and attach VORP."""
    scored = [{
        "name": p["name"],
        "position": p["position"],
        "espn_id": p["espn_id"],
        "points": round(score(p["stats"], league.scoring), 2),
    } for p in projections if p["position"] not in SKIP_POSITIONS]

    # Drop positions the league doesn't start at all
    started = set()
    for slot in league.starters:
        started |= SLOT_ELIGIBILITY.get(slot, {slot})
    scored = [p for p in scored if p["position"] in started]

    repl, used = replacement_levels(scored, league)
    for p in scored:
        p["replacement"] = round(repl.get(p["position"], 0.0), 2)
        p["vorp"] = round(p["points"] - p["replacement"], 2)

    scored.sort(key=lambda x: -x["vorp"])
    for i, p in enumerate(scored, 1):
        p["vbd_rank"] = i
    # positional rank by points
    seen = {}
    for p in sorted(scored, key=lambda x: -x["points"]):
        seen[p["position"]] = seen.get(p["position"], 0) + 1
        p["pos_rank"] = f"{p['position']}{seen[p['position']]}"

    league.replacement = repl
    league.starters_used = used
    return scored
