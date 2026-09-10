"""What everyone has ACTUALLY scored, live, from each platform.

Deliberately taken from the platform rather than computed here. Every league
scores differently and the platform is the authority on its own result -- a
number we derived ourselves would disagree with the app Carter is looking at,
and when those two disagree the one on the site is wrong by definition.

So this reads the live result and pairs it with our projection. The useful
question mid-game is not "how many points do I have" (the app tells you that)
but "am I ahead of where I should be, and is my opponent" -- which needs both
numbers side by side, and that is the thing no platform shows.

Cached for 30s: fantasy scores move on a scoring play, not continuously, and a
tighter poll just rate-limits you on a Sunday afternoon.
"""
from __future__ import annotations

import time

import requests

from .leagues import _env
from .names import key as nkey

SLEEPER = "https://api.sleeper.app/v1"
ESPN = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
UA = {"User-Agent": "Mozilla/5.0"}
_TTL = 30
_CACHE: dict = {}


def _cached(k, fn):
    now = time.time()
    hit = _CACHE.get(k)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    val = fn()
    _CACHE[k] = (now, val)
    return val


def clear() -> None:
    _CACHE.clear()


def _sleeper(league, week, blob):
    ms = requests.get(f"{SLEEPER}/league/{league.league_id}/matchups/{week}",
                      timeout=25).json() or []
    out = {}
    for m in ms:
        players = {}
        for pid, pts in (m.get("players_points") or {}).items():
            p = (blob or {}).get(str(pid)) or {}
            nm = p.get("full_name") or " ".join(
                x for x in (p.get("first_name"), p.get("last_name")) if x)
            pos = p.get("position")
            if not nm and str(pid).isalpha():        # team defense
                nm, pos = str(pid), "DEF"
            if nm:
                players[nkey(nm, "DST" if pos == "DEF" else pos)] = float(pts or 0)
        # `starters` is positionally aligned with `starters_points`, and only
        # starters count -- a bench player's points are not yours.
        started = {}
        sp = m.get("starters_points") or []
        for i, pid in enumerate(m.get("starters") or []):
            if not pid or pid == "0":
                continue
            p = (blob or {}).get(str(pid)) or {}
            nm = p.get("full_name") or " ".join(
                x for x in (p.get("first_name"), p.get("last_name")) if x)
            pos = p.get("position")
            if not nm and str(pid).isalpha():
                nm, pos = str(pid), "DEF"
            if nm:
                started[nkey(nm, "DST" if pos == "DEF" else pos)] = (
                    float(sp[i]) if i < len(sp) else 0.0)
        out[str(m.get("roster_id"))] = {
            "total": float(m.get("points") or 0),
            "players": players, "starters": started}
    return out


def _espn(league, week, blob):
    ck = {"espn_s2": _env("ESPN_S2"), "SWID": _env("ESPN_SWID")}
    url = (f"{ESPN}/seasons/2026/segments/0/leagues/{league.league_id}"
           f"?view=mMatchupScore&scoringPeriodId={week}")
    d = requests.get(url, headers=UA, cookies=ck, timeout=40).json()
    POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}
    out = {}
    for m in (d.get("schedule") or []):
        if m.get("matchupPeriodId") != week:
            continue
        for side in ("home", "away"):
            t = m.get(side) or {}
            tid = t.get("teamId")
            if tid is None:
                continue
            players, started = {}, {}
            roster = (t.get("rosterForCurrentScoringPeriod") or {})
            for e in (roster.get("entries") or []):
                pp = ((e.get("playerPoolEntry") or {}).get("player") or {})
                nm = pp.get("fullName")
                if not nm:
                    continue
                pos = POS.get(pp.get("defaultPositionId"))
                pts = 0.0
                for st in (pp.get("stats") or []):
                    if (st.get("scoringPeriodId") == week
                            and st.get("statSourceId") == 0):     # 0 = actual
                        pts = float(st.get("appliedTotal") or 0)
                k = nkey(nm, pos)
                players[k] = pts
                if e.get("lineupSlotId") not in (20, 21):
                    started[k] = pts
            out[str(tid)] = {
                "total": float(roster.get("appliedStatTotal")
                               or t.get("totalPoints") or 0),
                "players": players, "starters": started}
    return out


def _yahoo(league, week, blob):
    from . import yahoo
    lk = league.raw.get("league_key")
    d = yahoo.get(f"/league/{lk}/scoreboard;week={week}")
    out = {}
    try:
        node = d["fantasy_content"]["league"][1]["scoreboard"]["0"]["matchups"]
    except Exception:
        return out
    for k, v in node.items():
        if k == "count":
            continue
        for tk, tv in (v["matchup"]["0"]["teams"] or {}).items():
            if tk == "count":
                continue
            parts = tv["team"]
            info = {}
            for bit in parts[0]:
                if isinstance(bit, dict):
                    info.update(bit)
            pts = 0.0
            for bit in parts[1:]:
                if isinstance(bit, dict) and "team_points" in bit:
                    pts = float(bit["team_points"].get("total") or 0)
            out[str(info.get("team_id"))] = {
                "total": pts, "players": {}, "starters": {}}
    return out


def scores(league, week: int, blob: dict | None = None) -> dict:
    """{team_id: {total, players{(name,pos):pts}, starters{...}}}.

    Empty dict rather than an exception when a platform will not answer -- a
    dead scoreboard should grey out one number, not take down the page.
    """
    def go():
        try:
            if league.platform == "sleeper":
                return _sleeper(league, week, blob)
            if league.platform == "espn":
                return _espn(league, week, blob)
            if league.platform == "yahoo":
                return _yahoo(league, week, blob)
        except Exception:
            return {}
        return {}
    return _cached((league.name, week), go)


def any_started(games_list: list[dict]) -> bool:
    """Has anything kicked off? Decides whether to show live columns at all."""
    return any(g.get("state") in ("in", "post") for g in (games_list or []))


def team_states(games_list: list[dict]) -> dict:
    """{TEAM: 'pre'|'in'|'post'} for the week.

    Needed to tell two very different things apart that both look like zero: a
    player whose game has not kicked off yet, and a player who has played and
    scored nothing. Showing 0.0 for the first is a lie that reads as "he blanked"
    when the truth is "no information yet".
    """
    out = {}
    for g in (games_list or []):
        st = g.get("state") or "pre"
        for side in ("home", "away"):
            t = g.get(side)
            if t:
                out[str(t).upper()] = st
    return out


def has_played(team: str | None, states: dict) -> bool:
    """True once his game has started; False while it is still 'pre'."""
    from .starts import TEAM_ALIASES
    if not team:
        return False
    t = str(team).upper()
    return states.get(TEAM_ALIASES.get(t, t), states.get(t, "pre")) != "pre"


# A regulation game is four 15-minute quarters. Overtime is ignored on purpose:
# it is rare, and treating it as "0 remaining" is closer to right than pretending
# a fifth quarter of scoring is still coming.
_QUARTER = 900
_REGULATION = 4 * _QUARTER


def game_progress(games_list: list[dict]) -> dict:
    """{TEAM: fraction of his game still to be played}. 1.0 pre, 0.0 final.

    This is the whole basis of live odds: a player's remaining points carry
    uncertainty in proportion to the football left to play, and his banked
    points carry none at all.

    CAVEAT, and it is the weakest assumption in the live model: fantasy scoring
    is NOT uniform across the game clock. Garbage time inflates late passing,
    a blowout changes a back's carries, and a defense scores in lumps. Clock
    time is a proxy for opportunity remaining, not a measurement of it.
    """
    out = {}
    for g in (games_list or []):
        st = g.get("state")
        if st == "post":
            f = 0.0
        elif st == "in":
            period = int(g.get("period") or 1)
            clock = g.get("clock")
            clock = _QUARTER if clock is None else float(clock)
            elapsed = (min(period, 4) - 1) * _QUARTER + (_QUARTER - clock)
            f = 1.0 - elapsed / _REGULATION
            f = max(0.0, min(1.0, f))
        else:
            f = 1.0
        for side in ("home", "away"):
            t = g.get(side)
            if t:
                out[str(t).upper()] = f
    return out


def remaining(team: str | None, progress: dict) -> float:
    """Fraction of this player's game still to play; 1.0 if unknown."""
    from .starts import TEAM_ALIASES
    if not team:
        return 1.0
    t = str(team).upper()
    if t in progress:
        return progress[t]
    return progress.get(TEAM_ALIASES.get(t, t), 1.0)
