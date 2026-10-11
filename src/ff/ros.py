"""Put every projection source on ONE basis: rest-of-season, as a 17-game pace.

WHY THIS EXISTS. Once games are played the three season feeds stop meaning the
same thing, and the blend averaged them as if they did (found 2026-10-08):

    ESPN     its "season" line becomes REST OF SEASON. Equal, for all of the
             top 60, to the sum of its remaining weekly projections. Updated
             weekly, injury-aware.
    Sleeper  the season feed is the PRESEASON full season (`gp` = 18 for every
             player) and is never revised: Jaxson Dart, on IR after knee
             surgery, still carried 3,366 passing yards.
    FFToday  the same -- a preseason full season, never revised (Dart 3,570).

So Josh Allen's blend averaged ESPN's 285 rest-of-season points with two ~360
full-season totals, and Dart sat atop the waiver wire on a season he will not
play.

THE BASIS. Everything downstream treats `points` as a full-season total and
divides by 17 to get a per-game rate (`bakeoff.weekly_score`, which drives the
season odds; VORP; trades). So the honest common unit is:

    rest-of-season projection  x  17 / games his team has left

i.e. the per-scheduled-game rate he is projected at FROM HERE, expressed on the
17-game scale everything already expects. Before week 1 this is exactly the
old full-season number; after it, it is the same rate the market is now
pricing, with injuries and byes in it.

THE SOURCES, each turned into a rest-of-season stat line:

    ESPN     sum of its weekly projections from the current week through 18.
    Sleeper  the same, from its weekly feed (one request per week, cached 12h).
             Its future weeks ARE injury-aware: Dart reads zero for all of them
             and Travis Etienne zero until week 9.
    FFToday  publishes nothing weekly, so it can only say how GOOD a player is,
             not how AVAILABLE: its preseason per-game rate (season / 17) times
             the games ESPN and Sleeper still project him to play. Dart -> 0.

    Measured before keeping FFToday (weeks 1-4, 977 player-weeks actually
    played, reference PPR): its preseason rate scored MAE 5.27 against 5.21
    ESPN and 5.19 Sleeper weekly; adding it to their blend moved MAE by -0.026
    (95% CI -0.073 to +0.019). Not clearly better, not worse -- and that test
    cannot see the injured-player failure the availability gate exists for.

A source with no line for a player contributes nothing, exactly as before;
the blend is the per-stat mean over the sources that have him.
"""
from __future__ import annotations

import functools
import time

import requests

SEASON = 2026
LAST_WEEK = 18          # NFL regular season; the weekly feeds run through it
PACE_GAMES = 17         # the scale `points` has always been on (points / 17)


def current_week() -> int:
    """The week rest-of-season starts from. 1 before the season starts.

    Sleeper's state flips to the next week on Tuesday, the same day ESPN
    re-rolls its season line, so a week in progress is still counted whole --
    which is right for anyone whose game has not been played yet, and over-
    counts by at most part of one game for a Thursday player on Friday.
    """
    try:
        st = requests.get("https://api.sleeper.app/v1/state/nfl",
                          timeout=15).json()
        if str(st.get("season")) == str(SEASON):
            if st.get("season_type") == "regular":
                return max(1, int(st.get("week") or 1))
            if st.get("season_type") == "post":
                return LAST_WEEK + 1
    except Exception:
        pass
    return 1


@functools.lru_cache(maxsize=8)
def _games_left(week: int, _bucket: int) -> dict:
    """{nflverse team: regular-season games from `week` through 18}."""
    from .lineup import schedule
    s = schedule(SEASON)
    s = s[(s.week >= week) & (s.week <= LAST_WEEK)]
    out: dict = {}
    for t in list(s.home_team) + list(s.away_team):
        out[t] = out.get(t, 0) + 1
    return out


def games_left(team: str | None, week: int) -> int | None:
    from .starts import TEAM_ALIASES
    if not team:
        return None
    t = str(team).upper()
    return _games_left(week, int(time.time() // 86400)).get(TEAM_ALIASES.get(t, t))


def _sum(lines) -> dict:
    out: dict = {}
    for ln in lines:
        for k, v in (ln or {}).items():
            out[k] = out.get(k, 0.0) + float(v)
    return out


def _weekly_sleeper(week: int) -> dict:
    """{week: {(name,pos): line}} for every week from `week` on."""
    from .lineup import sleeper_weekly
    out = {}
    for w in range(week, LAST_WEEK + 1):
        try:
            out[w] = sleeper_weekly(w, max_age_hours=12)
        except Exception as e:
            print(f"  ! sleeper week {w} unavailable for rest-of-season: {e}")
    return out


def to_pace(merged: dict, week: int | None = None) -> dict:
    """Rewrite each merged record's per-source `lines` onto the common basis.

    `merged` is projections.fetch()'s {key: rec} with rec["lines"] holding each
    source's raw line and, for ESPN, rec["espn_weekly"] = {week: line}.
    Adds to each record:
        lines_raw      the feeds as published (kept for the lookup page)
        lines          each source's REST-OF-SEASON line
        ros_stats      their blend, rest of season (absolute)
        stats          ros_stats on the 17-game pace -- what everything scores
        games_left     his team's remaining scheduled games
        games_expected games ESPN/Sleeper project him to play (mean)
        basis_week     the week rest-of-season starts from
    """
    week = current_week() if week is None else week
    slp = _weekly_sleeper(week)
    slp_have = bool(slp)

    for k, rec in merged.items():
        raw = rec["lines"]
        rec["lines_raw"] = dict(raw)
        ros, active = {}, []

        ew = rec.get("espn_weekly")
        if "espn" in raw and ew is not None:
            wks = [ew.get(w) or ew.get(str(w)) for w in range(week, LAST_WEEK + 1)]
            ros["espn"] = _sum(wks)
            active.append(sum(1 for x in wks if x))
        elif "espn" in raw:
            # No weekly lines (an old cache): ESPN's season line IS rest of
            # season in-season, verified; availability unknown.
            ros["espn"] = dict(raw["espn"])

        if slp_have and "sleeper" in raw:
            wks = [slp[w].get(k) for w in slp]
            ros["sleeper"] = _sum(wks)
            active.append(sum(1 for x in wks if x))
        elif "sleeper" in raw and week <= 1:
            ros["sleeper"] = dict(raw["sleeper"])   # preseason: full season IS ROS

        exp_games = (sum(active) / len(active)) if active else None
        if "fftoday" in raw:
            if exp_games is not None:
                ros["fftoday"] = {s: v / PACE_GAMES * exp_games
                                  for s, v in raw["fftoday"].items()}
            elif week <= 1:
                ros["fftoday"] = dict(raw["fftoday"])
            # else: no updated source can vouch he is playing -> FFToday's
            # stale preseason line is the only evidence, and it is exactly
            # the evidence that put Dart on top of the wire. Left out.

        stats: dict = {}
        for line in ros.values():
            for s, v in line.items():
                stats.setdefault(s, []).append(v)
        ros_stats = {s: sum(v) / len(v) for s, v in stats.items()}

        g = games_left(rec.get("team"), week)
        if not g:
            # Unknown team (free agent) or season over: fall back to the
            # weeks left on the calendar, less a bye if one is still to come.
            g = max(1, LAST_WEEK - week + 1 - 1) if week <= LAST_WEEK else None
        scale = (PACE_GAMES / g) if g else 1.0

        rec["lines"] = ros
        rec["ros_stats"] = ros_stats
        rec["pace_stats"] = {s: v * scale for s, v in ros_stats.items()}
        rec["games_left"] = g
        rec["games_expected"] = exp_games
        rec["basis_week"] = week
    return merged
