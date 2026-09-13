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


# Points-allowed and yards-allowed tiers, as (lower, upper, canonical key).
# Upper bound None means open-ended.
PA_TIERS = [(0, 0, "pa_0"), (1, 6, "pa_1_6"), (7, 13, "pa_7_13"),
            (14, 20, "pa_14_20"), (21, 27, "pa_21_27"),
            (28, 34, "pa_28_34"), (35, None, "pa_35")]
YA_TIERS = [(0, 99, "ya_0_99"), (100, 199, "ya_100_199"),
            (200, 299, "ya_200_299"), (300, 399, "ya_300_399"),
            (400, 499, "ya_400_499"), (500, None, "ya_500")]

# League-average offensive output, used to extrapolate what a defense will have
# allowed by the final whistle. Rough figures, and stated as assumptions rather
# than measurements: ~22 points and ~340 yards per team per game.
AVG_PPG = 22.0
AVG_YPG = 340.0


def tier_value(amount: float | None, tiers, scoring: dict) -> float:
    """Points from a bucketed tier, or 0.0 where the league doesn't use them."""
    if amount is None:
        return 0.0
    for lo, hi, key_ in tiers:
        if amount >= lo and (hi is None or amount <= hi):
            return float(scoring.get(key_) or 0.0)
    return 0.0


def has_tiers(scoring: dict) -> bool:
    """True where this league scores defenses on points/yards-allowed tiers."""
    return any(k in scoring for _l, _h, k in PA_TIERS + YA_TIERS)


def dst_live(scoring: dict, scored: float, allowed_now, frac_left: float,
             pregame: float | None = None) -> float:
    """A defense's projected FINAL score, mid-game, without the phantom baseline.

    A defense is not one accumulating quantity but two of quite different kinds:

      * EVENTS -- sacks, interceptions, fumble recoveries, touchdowns. These
        accrue as the game goes, and what is banked stays banked.
      * STATE TIERS -- points allowed and yards allowed. These are a running
        verdict re-read every snap, and they can be TAKEN AWAY. At kickoff a
        defense has allowed nothing, so it sits in the top bucket of both:
        under this repo's Yahoo league that is 10 + 10 = 20 points credited
        before a snap is played, decaying to about zero in an average game.

    Treating the second kind as banked and adding a full pregame projection on
    top of it is what made a defense read 29.6 in the first quarter against a
    pregame 9.1. So: strip the tier points out of what is "scored" using the
    CURRENT state, project the final state forward at league-average rates, and
    re-price the tiers there. Events are left to accrue.

    `allowed_now` is (points, yards) conceded so far, or None before kickoff.
    Returns the pregame number unchanged when the league has no tiers (every
    platform but Yahoo here) or when nothing has kicked off.
    """
    pre = float(pregame or 0.0)
    if not has_tiers(scoring) or allowed_now is None:
        return float(scored) + pre * float(frac_left)

    pa_now, ya_now = allowed_now
    pa_now = float(pa_now or 0.0)
    # Yards can be missing even mid-game; fall back to the league-average pace
    # so the tier is priced off a plausible state rather than a flattering one.
    elapsed = max(0.0, 1.0 - float(frac_left))
    ya_now = float(ya_now) if ya_now is not None else AVG_YPG * elapsed

    tiers_now = (tier_value(pa_now, PA_TIERS, scoring)
                 + tier_value(ya_now, YA_TIERS, scoring))
    # Floored at zero: no event category in these leagues is worth negative
    # points, so a negative here means only that the score and the boxscore were
    # read a moment apart. Without the floor that skew lands in the projection.
    events_now = max(0.0, float(scored) - tiers_now)

    pa_end = pa_now + AVG_PPG * float(frac_left)
    ya_end = ya_now + AVG_YPG * float(frac_left)
    tiers_end = (tier_value(pa_end, PA_TIERS, scoring)
                 + tier_value(ya_end, YA_TIERS, scoring))

    # Events still to come. The pregame projection is a whole-game number that
    # already includes an expected tier value, so it cannot be used directly;
    # what is left of it after removing an average game's tiers is the event
    # part, and that is what accrues over the rest of the clock.
    avg_tiers = (tier_value(AVG_PPG, PA_TIERS, scoring)
                 + tier_value(AVG_YPG, YA_TIERS, scoring))
    event_rate = max(0.0, pre - avg_tiers)
    return events_now + event_rate * float(frac_left) + tiers_end


def live_points(scoring: dict, position: str | None, scored: float,
                pregame: float | None, frac_left: float,
                allowed_now=None) -> float:
    """One player's projected final score mid-game. THE live-projection rule.

    Everything that shows a "proj now" anywhere in the app goes through here, so
    a player cannot read one number on the lineup page and another on the
    scoreboard. Ordinary players accrue linearly; defenses do not, and get the
    tier-aware treatment in dst_live().
    """
    pos = (position or "").upper()
    pos = "DST" if pos in ("DEF", "D/ST") else pos
    if pos == "DST":
        return dst_live(scoring, scored, allowed_now, frac_left, pregame)
    return float(scored) + float(pregame or 0.0) * float(frac_left)
