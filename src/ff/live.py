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


def watch_ranking(games_list: list[dict], holdings: dict) -> list[dict]:
    """Rank games by how much of YOUR season is actually in them.

    `holdings` maps NFL team -> list of {player, league, starter}. A starter is
    worth more than a bench player because he is the one whose points you keep,
    and the same player rostered in three leagues counts three times -- that is
    genuinely three times as much of your Sunday riding on him.
    """
    out = []
    for g in games_list:
        involved = []
        for side in ("home", "away"):
            for h in holdings.get(g[side], []):
                involved.append({**h, "nfl_team": g[side]})
        if not involved:
            continue
        starters = sum(1 for h in involved if h.get("starter"))
        score = starters * 2 + (len(involved) - starters)
        out.append({**g, "players": sorted(
            involved, key=lambda h: (not h.get("starter"), h["player"])),
            "n_players": len(involved), "n_starters": starters, "watch_score": score})
    return sorted(out, key=lambda g: (-g["watch_score"], g["kickoff"] or ""))


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


def my_holdings(leagues, week: int, blob=None) -> dict:
    """{NFL team: [{player, position, league, starter}]} across ALL your leagues.

    Cross-league on purpose. Your Sunday isn't organised by league -- it's
    organised by which games are on, and a player you roster three times is
    three times as much of your afternoon.
    """
    from . import rosters
    from .lineup import optimize, weekly_points
    from .names import key as nkey

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
        try:
            wpts = weekly_points(week, L.scoring)
        except Exception:
            wpts = {}
        pool = []
        for p in mine.players:
            pts, *_ = wpts.get(nkey(p["name"], p.get("position")), (0.0, 0, 0.0))
            pool.append({**p, "week_points": pts})
        try:
            filled, _ = optimize(pool, L)
            starting = {p["name"] for slot in filled.values() for p in slot}
        except Exception:
            starting = set()
        for p in pool:
            team = _norm(p.get("team"))
            if not team:
                continue
            out.setdefault(team, []).append({
                "player": p["name"], "position": p.get("position"),
                "league": L.name, "starter": p["name"] in starting,
                "proj": round(p.get("week_points") or 0, 1)})
    return out
