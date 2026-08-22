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
    scored = []
    for p in projections:
        if p["position"] in SKIP_POSITIONS:
            continue
        # Score each source separately too: where they disagree, confidence is low.
        per_source = {src: score(line, league.scoring)
                      for src, line in (p.get("source_lines") or {}).items()}
        spread = (max(per_source.values()) - min(per_source.values())
                  if len(per_source) > 1 else 0.0)
        # Raw point spread scales with volume, so it just ranks high scorers and
        # picks up Sleeper's systematic ~5-8% discount. Relative spread measures
        # actual disagreement.
        mean_pts = (sum(per_source.values()) / len(per_source)) if per_source else 0.0
        rel_spread = (spread / mean_pts) if mean_pts > 20 else 0.0
        scored.append({
            "name": p["name"],
            "position": p["position"],
            "espn_id": p.get("espn_id"),
            "team": p.get("team"),
            "points": round(score(p["stats"], league.scoring), 2),
            "n_sources": p.get("n_sources", 1),
            "spread": round(spread, 1),
            "rel_spread": round(rel_spread, 3),
            "per_source": {k: round(v, 1) for k, v in per_source.items()},
        })

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
        p["pos_rank_n"] = seen[p["position"]]
        p["pos_rank"] = f"{p['position']}{seen[p['position']]}"

    league.replacement = repl
    league.starters_used = used
    return scored


def rosterable_depth(league) -> dict:
    """How deep at each position the league can plausibly roster.

    VORP alone puts TE13-TE30 above WR45 late in a draft: TE has a low
    replacement level and a 133-man tail, so a barely-startable TE keeps
    out-ranking a WR who is genuinely worth a bench spot. But only ~1 TE per
    team ever gets rostered, so most of that tail is unpickable in practice
    and just crowds the board.

    Cap each position at league-wide starter demand plus a share of the bench,
    allocated in proportion to how often the position actually starts. That is
    what "rosterable" means: nobody drafts a 3rd TE in a 1-TE league.
    """
    used = league.starters_used or {}
    total_started = sum(used.values()) or 1
    bench_pool = league.teams * league.bench
    depth = {}
    for pos, n in used.items():
        share = n / total_started
        # Slack scales with the position's own share, not a flat round. A flat
        # +teams is nothing for WR and enormous for TE, which is how TE12-TE30
        # got back onto the late board.
        slack = max(2, round(league.teams * share))
        # Floor at one per team: even a position that loses its slot battle to
        # another (TE vs WR in a combo WR/TE slot) still gets rostered ~1 deep
        # per team, so the board must carry that many.
        depth[pos] = max(league.teams,
                         int(round(n + bench_pool * share + slack)))
    return depth
