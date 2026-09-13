"""Live NFL games, and which of them are worth your Sunday.

Two questions this answers:

  What's happening right now -- scores, clock, and how your players are doing
  in each game, across every league at once.

  What should I put on -- you have players scattered across five leagues and
  four TVs' worth of games. The game with three of your starters in it beats
  the one with a single bench flier, and that ranking isn't obvious at a glance
  on Sunday morning.

Source is ESPN's public scoreboard, which is the only free feed carrying the
broadcast network. nflverse has kickoff times but no TV listing at all.
"""
from __future__ import annotations

import datetime as dt
import functools
import time

import requests

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"
UA = {"User-Agent": "Mozilla/5.0"}

# ESPN's abbreviations vs everyone else's.
ALIAS = {"WSH": "WAS", "LAR": "LA", "JAX": "JAC"}


def _norm(t: str | None) -> str:
    t = (t or "").upper()
    return ALIAS.get(t, t)


def games(week: int | None = None, season: int = 2026,
          date: str | None = None, ttl_seconds: int = 30) -> list[dict]:
    """Games for a week (or a date), with kickoff, network, status and score."""
    params = {"limit": "100"}
    if date:
        params["dates"] = date
    else:
        params.update({"week": str(week), "seasontype": "2", "dates": str(season)})
    r = requests.get(SCOREBOARD, params=params, headers=UA, timeout=25)
    r.raise_for_status()
    out = []
    for e in r.json().get("events") or []:
        c = (e.get("competitions") or [{}])[0]
        status = (c.get("status") or {}).get("type") or {}
        comp = {x.get("homeAway"): x for x in (c.get("competitors") or [])}
        home, away = comp.get("home") or {}, comp.get("away") or {}
        nets = []
        for b in (c.get("broadcasts") or []):
            nets += b.get("names") or []
        out.append({
            "id": e.get("id"),
            "kickoff": e.get("date"),
            "home": _norm((home.get("team") or {}).get("abbreviation")),
            "away": _norm((away.get("team") or {}).get("abbreviation")),
            "home_score": int(home.get("score") or 0),
            "away_score": int(away.get("score") or 0),
            "network": ", ".join(dict.fromkeys(nets)) or "—",
            "state": status.get("state"),           # pre | in | post
            "detail": status.get("shortDetail"),
            # Clock, for pricing how much of a player's game is still to come.
            # `clock` is seconds left in the CURRENT period, not the game.
            "period": (c.get("status") or {}).get("period"),
            "clock": (c.get("status") or {}).get("clock"),
            "completed": bool(status.get("completed")),
        })
    return sorted(out, key=lambda g: (g["kickoff"] or "", g["home"]))


def kickoff_local(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return t.strftime("%a %-I:%M %p")
    except Exception:
        return iso


def watch_ranking(games_list: list[dict], holdings: dict,
                  starters_only: bool = True) -> list[dict]:
    """Games with your players in them, most of your season first.

    Starters only by default: a bench player's points are not yours this week,
    so he is not a reason to put a game on. The same player rostered in three
    leagues counts three times -- that is genuinely three times as much riding
    on him.
    """
    out = []
    for g in games_list:
        involved = []
        for side in ("home", "away"):
            for h in holdings.get(g[side], []):
                if starters_only and not h.get("starter"):
                    continue
                involved.append({**h, "nfl_team": g[side]})
        if not involved:
            continue
        out.append({**g, "players": sorted(involved, key=lambda h: -h["proj"]),
                    "n_players": len(involved),
                    "proj_total": round(sum(h["proj"] for h in involved), 1)})
    # Ties on headcount are broken by projected points -- two starters worth 30
    # is a better watch than two worth 12.
    return sorted(out, key=lambda g: (-g["n_players"], -g["proj_total"]))


def slot_label(iso: str | None) -> str:
    if not iso:
        return "TBD"
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except Exception:
        return iso
    return t.strftime("%a %-I:%M %p")


def _slot_key(iso: str | None):
    """Games kicking off in the same hour are the same slot (4:05 and 4:25)."""
    if not iso:
        return ("zzz", 0)
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except Exception:
        return (iso, 0)
    return (t.strftime("%Y-%m-%d"), t.hour)


def watch_by_slot(games_list: list[dict], holdings: dict,
                  starters_only: bool = True) -> list[dict]:
    """One recommendation per time slot, because you can only watch one at a time.

    A single ranking for the whole week is the wrong shape: the best game
    Sunday afternoon tells you nothing about what to put on Sunday night. So
    games are grouped into their kickoff slot and ranked within it, and each
    slot names its own pick.
    """
    ranked = watch_ranking(games_list, holdings, starters_only)
    # Keep slots that have no holdings out entirely, but remember every game in
    # a slot we do care about, so "nothing of yours is on" stays visible.
    slots: dict = {}
    for g in ranked:
        slots.setdefault(_slot_key(g["kickoff"]), []).append(g)
    out = []
    for k in sorted(slots):
        gs = slots[k]
        out.append({
            "slot": slot_label(gs[0]["kickoff"]),
            "kickoff": gs[0]["kickoff"],
            "pick": gs[0],
            "others": gs[1:],
            "n_games": len(gs),
        })
    return out


@functools.lru_cache(maxsize=64)
def _summary(event_id: str) -> dict:
    r = requests.get(SUMMARY, params={"event": event_id}, headers=UA, timeout=25)
    r.raise_for_status()
    return r.json()


def player_lines(event_id: str) -> dict:
    """{normalised player name: one-line stat summary} for a live/finished game.

    Best-effort: ESPN's box score shape varies by game state, and a missing
    line is not worth failing a dashboard over.
    """
    try:
        d = _summary(event_id)
    except Exception:
        return {}
    out = {}
    for team in (d.get("boxscore") or {}).get("players") or []:
        for cat in team.get("statistics") or []:
            labels = cat.get("labels") or []
            for a in cat.get("athletes") or []:
                nm = ((a.get("athlete") or {}).get("displayName") or "").strip()
                if not nm:
                    continue
                stats = a.get("stats") or []
                bits = [f"{v} {l.lower()}" for l, v in zip(labels, stats)
                        if v not in ("0", "", None)][:3]
                if bits:
                    out.setdefault(nm, []).append(
                        f"{cat.get('name','')}: " + ", ".join(bits))
    return {k: " · ".join(v) for k, v in out.items()}


def _team_rows(L, team, week, blob, extra) -> dict:
    """One fantasy team's roster, grouped by the NFL team each player is on.

    Built through `lineup.build_pool` rather than `weekly_points` directly:
    weekly_points carries no defenses at all (they are priced in
    `special.dst_week`), so every D/ST on this page projected 0.0 and the
    Sunday numbers disagreed with the lineup and matchup pages.

    `starter` is the lineup actually SET on the platform, falling back to the
    optimiser only where the platform publishes no starters -- the same
    preference the rest of the app makes. Using the optimiser's answer meant
    this page could call a man a starter while your real lineup benched him.
    """
    from .lineup import build_pool, optimize
    from .names import key as nkey

    def _pos(q):
        pos = (q.get("position") or "").upper()
        return "DST" if pos in ("DEF", "D/ST") else pos

    pool = build_pool(L, team.players, week, None, blob)
    starters = {nkey(q["name"], _pos(q)) for q in (team.starters or [])
                if q.get("name")}
    if not starters:
        try:
            filled, _ = optimize(pool, L)
            starters = {nkey(p["name"], p["position"])
                        for v in filled.values() for p in v}
        except Exception:
            starters = set()

    out = {}
    for p in pool:
        nfl = _norm(p.get("team"))
        if not nfl:
            continue
        k = nkey(p["name"], p.get("position"))
        out.setdefault(nfl, []).append({
            "player": p["name"], "position": p.get("position"),
            "league": L.name,
            "starter": k in starters,
            "proj": round(p.get("week_points") or 0, 1),
            "nfl_team": nfl,
            # Carried so the caller can join this row to a live score without
            # re-reading the roster: the platform's team id, and the name key
            # its per-player scores are filed under.
            "team_id": getattr(team, "team_id", None), "key": k, **extra})
    return out


def holdings(leagues, week: int, blob=None, sides=("mine",)) -> dict:
    """{NFL team: [player rows]} across ALL your leagues, optionally both sides.

    Cross-league on purpose. Your Sunday isn't organised by league -- it's
    organised by which games are on, and a player you roster three times is
    three times as much of your afternoon.

    With "opp" in `sides` each league's current opponent is included too, tagged
    `side="opp"`, so a game can be read as what it actually is: some of it
    helping you and some of it beating you. Guillotine leagues contribute no
    opponent rows -- there is no rival there, you play the field.
    """
    from . import rosters

    out = {}
    for L in leagues:
        try:
            teams = rosters.all_teams(L, week, blob)
        except Exception:
            continue
        if not rosters.has_drafted(L, teams):
            continue
        mine = next((t for t in teams if t.mine), None)
        if not mine:
            continue
        want = []
        if "mine" in sides:
            want.append((mine, {"side": "mine", "who": "you"}))
        if "opp" in sides:
            try:
                opp = rosters.opponent_of(L, week, teams)
            except Exception:
                opp = None
            if opp is not None:
                want.append((opp, {"side": "opp", "who": opp.name}))
        for team, extra in want:
            try:
                rows = _team_rows(L, team, week, blob, extra)
            except Exception:
                continue
            for nfl, rs in rows.items():
                out.setdefault(nfl, []).extend(rs)
    return out


def my_holdings(leagues, week: int, blob=None) -> dict:
    """Your players only. Thin wrapper -- see holdings()."""
    return holdings(leagues, week, blob, sides=("mine",))


@functools.lru_cache(maxsize=64)
def _summary(event_id: str, _bucket: int) -> dict:
    r = requests.get(SUMMARY, params={"event": event_id}, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()


def allowed(games_list: list[dict], ttl_seconds: int = 45) -> dict:
    """{TEAM: (points allowed, yards allowed)} so far, for IN-PROGRESS games.

    A defense's points-allowed and yards-allowed tiers are not accrued the way a
    sack is -- they are a running verdict on the OPPONENT's game, re-read every
    snap. To project them you need the opponent's current score and yardage,
    which is what this fetches.

    Only live games are requested: a finished game's tiers are already settled in
    the score, and a game that hasn't kicked off has nothing to report.
    """
    bucket = int(time.time() // max(1, ttl_seconds))
    out = {}
    for g in (games_list or []):
        if g.get("state") != "in" or not g.get("id"):
            continue
        try:
            js = _summary(str(g["id"]), bucket)
        except Exception:
            continue
        yards = {}
        for t in (js.get("boxscore") or {}).get("teams") or []:
            ab = _norm((t.get("team") or {}).get("abbreviation"))
            for st in (t.get("statistics") or []):
                if st.get("name") == "totalYards":
                    try:
                        yards[ab] = float(str(st.get("displayValue") or 0).replace(",", ""))
                    except ValueError:
                        pass
        home, away = g.get("home"), g.get("away")
        # A defense ALLOWS what the other side has produced.
        out[home] = (float(g.get("away_score") or 0), yards.get(away))
        out[away] = (float(g.get("home_score") or 0), yards.get(home))
    return out
