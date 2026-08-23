"""Kickers and defenses — the two slots that get forgotten at the end.

They are deliberately absent from the main board: neither is flex-eligible, so
excluding them has no effect on anyone else's replacement level, and their
projections are the weakest we have. But a draft board that silently omits two
starting slots lets you reach the last round having filled neither, which is
exactly what happened.

So they live here, ranked on the best signal actually available rather than
pretended into the VBD engine:

  DST  the sum of projected points over the first few weeks. A defense is a
       streaming slot -- you are not drafting a season, you are drafting a
       favourable September and replacing him after. Sleeper publishes weekly
       DST projections with the opponent attached, which is precisely that.

  K    season projection from ESPN, which is the only source that carries
       kickers at all.

HONEST LIMIT: these are the sources' own scoring, not your league's. Sleeper
leagues expose no K/DST scoring rules through the API at all, and DST scoring
is bucketed (points-allowed tiers) in a way that does not survive translation.
Treat the ordering as a shortlist, not a valuation -- the gap between DST3 and
DST8 is inside anyone's error bars.
"""
from __future__ import annotations

import functools

import requests

SEASON = 2026
SLEEPER_PROJ = ("https://api.sleeper.app/projections/nfl/{yr}/{wk}"
                "?season_type=regular&position[]={pos}&order_by=ppr")
UA = {"User-Agent": "Mozilla/5.0"}


@functools.lru_cache(maxsize=4)
def dst_early(weeks: int = 3, season: int = SEASON) -> list[dict]:
    """Defenses ranked by projected points over the first `weeks` weeks."""
    agg: dict[str, dict] = {}
    for wk in range(1, weeks + 1):
        try:
            r = requests.get(SLEEPER_PROJ.format(yr=season, wk=wk, pos="DEF"),
                             headers=UA, timeout=20)
            r.raise_for_status()
            rows = r.json()
        except Exception:
            continue                      # a missing week shouldn't kill the list
        for x in rows:
            team = x.get("team")
            if not team:
                continue
            st = x.get("stats") or {}
            pts = st.get("pts_half_ppr")
            if pts is None:
                pts = st.get("pts_std") or 0.0
            # Drafts record a defense as "Los Angeles Rams", not "LAR", so
            # keep the nickname from this same feed -- it is the only token
            # that reliably appears in both.
            pl = x.get("player") or {}
            nick = (pl.get("last_name") or pl.get("first_name") or "").strip()
            e = agg.setdefault(team, {"team": team, "pts": 0.0, "opps": [],
                                      "weeks": 0, "nick": nick})
            if nick and not e.get("nick"):
                e["nick"] = nick
            e["pts"] += float(pts)
            e["opps"].append(f"w{wk} {x.get('opponent') or '?'}")
            e["weeks"] += 1
    out = sorted(agg.values(), key=lambda e: -e["pts"])
    for e in out:
        e["pts"] = round(e["pts"], 1)
        e["name"] = f"{e['team']} D/ST"
    return out


def kickers(projections: list[dict], league) -> list[dict]:
    """Kickers, scored under the league's rules where it publishes any."""
    from .stats import score
    out = []
    for p in projections:
        if p.get("position") != "K":
            continue
        pts = score(p.get("stats") or {}, league.scoring)
        out.append({"name": p["name"], "team": p.get("team"),
                    "pts": round(pts, 1)})
    # Leagues that publish no kicker scoring score everyone 0; fall back to the
    # source's own order rather than presenting a meaningless tie.
    if out and all(k["pts"] == 0 for k in out):
        for i, k in enumerate(out):
            k["pts"] = None
        return out
    return sorted(out, key=lambda k: -(k["pts"] or 0))
