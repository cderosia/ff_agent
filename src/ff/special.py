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

import time

import requests

SEASON = 2026
SLEEPER_PROJ = ("https://api.sleeper.app/projections/nfl/{yr}/{wk}"
                "?season_type=regular&position[]={pos}&order_by=ppr")
UA = {"User-Agent": "Mozilla/5.0"}

# `functools.lru_cache` was wrong here and it hid for weeks: it is keyed only on
# the arguments and never expires, so in a long-running app (the Streamlit site
# stays up for days) a defense's projection was frozen at whatever it was the
# first time the process asked. "Refresh data" could not shift it either --
# nothing clears an lru_cache. A TTL cache keeps the network savings and still
# lets a number move on game day, and `cache_clear` stays available so the
# refresh button can force it.
_TTL_SECONDS = 900


def _ttl_cache(fn):
    store: dict = {}

    @functools.wraps(fn)
    def wrapper(*args):
        now = time.time()
        hit = store.get(args)
        if hit and now - hit[0] < _TTL_SECONDS:
            return hit[1]
        val = fn(*args)
        store[args] = (now, val)
        return val

    wrapper.cache_clear = store.clear
    return wrapper


@_ttl_cache
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
    """Kickers, scored under the league's rules as closely as the data allows.

    Three cases, and the returned `basis` says which one you got:

      "league"       the league publishes made-FG buckets that line up with
                     ESPN's projection, so this is a real scoring of it
      "approx"       the league scores by FG DISTANCE (Sleeper's fgm_yds) while
                     ESPN reports only buckets. Bucket midpoints are used --
                     30, 45 and 53 yards -- which is an approximation, but a
                     far better ordering than the alternative
      "unscored"     nothing usable; ESPN's own order, which is not a ranking

    Before this, every Sleeper league fell through to "unscored" and the kicker
    list was just whatever order the source happened to return.
    """
    from .stats import score
    sc = league.scoring
    per_yard = sc.get("fg_yds", 0.0)
    bucketed = any(k.startswith("fg_made") for k in sc)

    out = []
    for p in projections:
        if p.get("position") != "K":
            continue
        st = p.get("stats") or {}
        if bucketed:
            pts, basis = score(st, sc), "league"
        elif per_yard:
            # ESPN gives counts per bucket; distance scoring needs yards, so
            # take the midpoint of each band.
            pts = (st.get("fg_made_0_39", 0) * 30 * per_yard
                   + st.get("fg_made_40_49", 0) * 45 * per_yard
                   + st.get("fg_made_50", 0) * 53 * per_yard
                   + st.get("xp_made", 0) * sc.get("xp_made", 1.0)
                   + st.get("fg_missed", 0) * sc.get("fg_missed", 0.0))
            basis = "approx"
        else:
            pts, basis = None, "unscored"
        out.append({"name": p["name"], "team": p.get("team"),
                    "pts": round(pts, 1) if pts is not None else None,
                    "basis": basis})
    if any(k["pts"] for k in out):
        out.sort(key=lambda k: -(k["pts"] or 0))
    return out



@_ttl_cache
def dst_week(week: int, season: int = SEASON) -> dict:
    """{team code: projected points} for defenses in one week.

    The weekly blend that drives lineups and win probability carries no
    defenses at all, so every team's DST scored 0 and every projection ran
    about a defense light. That washes out when comparing teams -- everyone
    starts one -- but it understates absolute totals, and in a guillotine
    league the absolute number is what decides who goes home.
    """
    try:
        r = requests.get(SLEEPER_PROJ.format(yr=season, wk=week, pos="DEF"),
                         headers=UA, timeout=20)
        r.raise_for_status()
        rows = r.json()
    except Exception:
        return {}
    out = {}
    for x in rows:
        team = x.get("team")
        st = x.get("stats") or {}
        pts = st.get("pts_half_ppr", st.get("pts_std"))
        if team and pts is not None:
            out[team.upper()] = float(pts)
    return out
