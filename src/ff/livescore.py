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

import functools
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
    """Live actuals from mRoster, NOT mMatchupScore.

    mMatchupScore's roster entries carry only a lineup slot and a bare stats
    array -- no player id and no name -- so every entry was unidentifiable and
    silently skipped, leaving ESPN with a team total and nothing else. mRoster
    with an explicit scoringPeriodId returns the same live numbers WITH names
    and slots: `statSourceId` 0 is the actual, 1 the projection.
    """
    ck = {"espn_s2": _env("ESPN_S2"), "SWID": _env("ESPN_SWID")}
    url = (f"{ESPN}/seasons/2026/segments/0/leagues/{league.league_id}"
           f"?view=mRoster&view=mTeam&scoringPeriodId={week}")
    d = requests.get(url, headers=UA, cookies=ck, timeout=40).json()
    POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}
    out = {}
    for t in (d.get("teams") or []):
        players, started = {}, {}
        total = 0.0
        for e in ((t.get("roster") or {}).get("entries") or []):
            pp = ((e.get("playerPoolEntry") or {}).get("player") or {})
            nm = pp.get("fullName")
            if not nm:
                continue
            pos = POS.get(pp.get("defaultPositionId"))
            pts = 0.0
            for stt in (pp.get("stats") or []):
                if (stt.get("scoringPeriodId") == week
                        and stt.get("statSourceId") == 0):     # 0 = actual
                    pts = float(stt.get("appliedTotal") or 0)
            k = nkey(nm, pos)
            players[k] = pts
            if e.get("lineupSlotId") not in (20, 21):          # bench / IR
                started[k] = pts
                total += pts
        out[str(t.get("id"))] = {"total": round(total, 2),
                                 "players": players, "starters": started}
    return out


def _yahoo_roster_node(node) -> tuple[dict, dict]:
    """Parse one team's `players` node into (all players, starters) -> points."""
    players, started = {}, {}
    for k, v in (node or {}).items():
        if k == "count":
            continue
        pl = v["player"]
        info = {}
        for bit in pl[0]:
            if isinstance(bit, dict):
                info.update(bit)
        nm = (info.get("name") or {}).get("full")
        pos = info.get("display_position")
        if not nm:
            continue
        pts, slot = 0.0, None
        for bit in pl[1:]:
            if not isinstance(bit, dict):
                continue
            if "player_points" in bit:
                pts = float(bit["player_points"].get("total") or 0)
            if "selected_position" in bit:
                for s_ in bit["selected_position"]:
                    if isinstance(s_, dict) and "position" in s_:
                        slot = s_["position"]
        kk = nkey(nm, "DST" if pos == "DEF" else pos)
        players[kk] = pts
        if slot and slot not in ("BN", "IR", "IL"):
            started[kk] = pts
    return players, started


def _yahoo_all_rosters(league, week: int) -> dict:
    """{team_id: (players, starters)} for EVERY team, in one request.

    Was one HTTP call per team. At 16 teams that was ~11 seconds of the app's
    cold start -- more than every other league combined -- for data Yahoo will
    serve in a single round trip at `/league/{key}/teams/roster/players/stats`.
    Measured: 16 sequential calls 10.9s, one batched call 0.6s.
    """
    from . import yahoo
    lk = league.raw.get("league_key")
    out = {}
    try:
        d = yahoo.get(f"/league/{lk}/teams/roster/players/stats"
                      f";type=week;week={week}")
        node = d["fantasy_content"]["league"][1]["teams"]
    except Exception:
        return out
    for k, v in (node or {}).items():
        if k == "count":
            continue
        try:
            parts = v["team"]
            info = {}
            for bit in parts[0]:
                if isinstance(bit, dict):
                    info.update(bit)
            tid = str(info.get("team_id"))
            roster = None
            for bit in parts[1:]:
                if isinstance(bit, dict) and "roster" in bit:
                    roster = bit["roster"]["0"]["players"]
            if tid and roster is not None:
                out[tid] = _yahoo_roster_node(roster)
        except Exception:
            continue          # one unreadable team must not lose the other 15
    return out


def _yahoo(league, week, blob):
    from . import yahoo
    lk = league.raw.get("league_key")
    rosters_by_team = _yahoo_all_rosters(league, week)
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
            tid = str(info.get("team_id"))
            # Already fetched for every team above; the totals still work
            # if a breakdown is missing.
            players, started = rosters_by_team.get(tid, ({}, {}))
            out[tid] = {"total": pts, "players": players, "starters": started}
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


@functools.lru_cache(maxsize=1)
def _rev_aliases() -> dict:
    from .starts import TEAM_ALIASES
    out = {}
    for k, v in TEAM_ALIASES.items():
        out.setdefault(v, []).append(k)
    return out


def _codes(team: str | None) -> list[str]:
    """Every spelling of one NFL team, so a lookup resolves whichever the other
    side happened to use.

    The two alias tables in this repo disagree in DIRECTION: `starts.TEAM_ALIASES`
    maps JAC -> JAX, while `live.ALIAS` maps JAX -> JAC. Game states are keyed by
    the latter, so a roster saying "Jax" (Yahoo spells it that way) translated to
    JAX, missed the JAC key entirely, and every Jaguar read as though his game had
    not kicked off -- a defense sitting on 21.5 points showed a dash all afternoon.
    Resolving BOTH directions here means neither table has to win.
    """
    from .starts import TEAM_ALIASES
    t = str(team or "").upper()
    if not t:
        return []
    seen, out = set(), []
    for c in [t, TEAM_ALIASES.get(t)] + _rev_aliases().get(t, []):
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out



def pick(team: str | None, d: dict, default=None):
    """Look one NFL team up in a team-keyed dict, trying every spelling.

    Public because callers outside this module keep re-deriving it from
    `starts.TEAM_ALIASES` and getting the direction wrong -- which is exactly
    how a Jacksonville defense went on reading its phantom 20-point baseline
    after the alias bug was supposedly fixed.
    """
    for c in _codes(team):
        if c in d:
            return d[c]
    return default

def has_played(team: str | None, states: dict) -> bool:
    """True once his game has started; False while it is still 'pre'."""
    for c in _codes(team):
        if c in states:
            return states[c] != "pre"
    return False


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
    for c in _codes(team):
        if c in progress:
            return progress[c]
    return 1.0


def is_playing(team: str | None, states: dict) -> bool:
    """On the field RIGHT NOW -- distinct from has_played(), which stays true
    after the final whistle. Drives the live dot, which should go out when the
    game ends rather than implying he is still accumulating."""
    for c in _codes(team):
        if c in states:
            return states[c] == "in"
    return False


def game_period(games_list: list[dict]) -> dict:
    """{TEAM: (state, period)} -- the quarter, not just whether it is live.

    `game_progress` gives a fraction, which is the right thing for projecting
    but the wrong thing for "is he in the second half": a fraction blurs the
    half boundary, and a rule about halves should read the quarter directly.
    """
    out = {}
    for g in (games_list or []):
        st = g.get("state") or "pre"
        per = int(g.get("period") or 0)
        for side in ("home", "away"):
            t = g.get(side)
            if t:
                out[str(t).upper()] = (st, per)
    return out


def second_half(team: str | None, periods: dict) -> bool:
    """His game is live and past halftime (3rd quarter or later)."""
    st, per = pick(team, periods, ("pre", 0))
    return st == "in" and per >= 3
