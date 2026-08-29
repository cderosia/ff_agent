"""Every team's roster, from any platform, in one shape.

Win probability needs more than your own team: a head-to-head week needs your
opponent, and a guillotine week needs all sixteen. Each platform answers that
differently, so the difference is absorbed here and nothing downstream has to
know which site a league lives on.

Returns a list of Team records. `mine` marks yours; `opponent_of` resolves the
week's matchup where the league has one.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import requests

from .leagues import _env

UA = {"User-Agent": "Mozilla/5.0"}
SLEEPER = "https://api.sleeper.app/v1"
ESPN = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"


@dataclass
class Team:
    team_id: str
    name: str
    mine: bool = False
    players: list = field(default_factory=list)   # [{name, position, team}]


# ---------------------------------------------------------------------------
# Sleeper
# ---------------------------------------------------------------------------
def _sleeper(league, blob) -> list[Team]:
    lid = league.league_id
    rosters = requests.get(f"{SLEEPER}/league/{lid}/rosters", timeout=30).json()
    users = {u["user_id"]: u for u in
             requests.get(f"{SLEEPER}/league/{lid}/users", timeout=30).json()}
    me = str(league.raw.get("owner_id") or "")
    out = []
    for r in rosters:
        owner = str(r.get("owner_id") or "")
        u = users.get(owner) or {}
        players = []
        for pid in (r.get("players") or []):
            p = blob.get(str(pid)) or {}
            nm = p.get("full_name") or " ".join(
                x for x in (p.get("first_name"), p.get("last_name")) if x)
            if nm:
                players.append({"name": nm, "position": p.get("position"),
                                "team": p.get("team")})
        out.append(Team(team_id=str(r.get("roster_id")),
                        name=(u.get("metadata") or {}).get("team_name")
                             or u.get("display_name") or f"team {r.get('roster_id')}",
                        mine=bool(me and owner == me), players=players))
    return out


def _sleeper_opponent(league, week, teams) -> str | None:
    mine = next((t for t in teams if t.mine), None)
    if not mine:
        return None
    ms = requests.get(f"{SLEEPER}/league/{league.league_id}/matchups/{week}",
                      timeout=30).json() or []
    mid = next((m.get("matchup_id") for m in ms
                if str(m.get("roster_id")) == mine.team_id), None)
    if mid is None:
        return None
    for m in ms:
        if m.get("matchup_id") == mid and str(m.get("roster_id")) != mine.team_id:
            return str(m.get("roster_id"))
    return None


# ---------------------------------------------------------------------------
# ESPN
# ---------------------------------------------------------------------------
def _espn(league, week) -> list[Team]:
    ck = {"espn_s2": _env("ESPN_S2"), "SWID": _env("ESPN_SWID")}
    url = (f"{ESPN}/seasons/2026/segments/0/leagues/{league.league_id}"
           f"?view=mRoster&view=mTeam")
    d = requests.get(url, headers=UA, cookies=ck, timeout=40).json()
    swid = (_env("ESPN_SWID") or "").strip("{}").lower()
    out = []
    for t in d.get("teams") or []:
        mine = any(str(o).strip("{}").lower() == swid for o in (t.get("owners") or []))
        players = []
        for e in ((t.get("roster") or {}).get("entries") or []):
            pp = ((e.get("playerPoolEntry") or {}).get("player") or {})
            nm = pp.get("fullName")
            if nm:
                players.append({"name": nm,
                                "position": _ESPN_POS.get(pp.get("defaultPositionId")),
                                "team": None})
        out.append(Team(team_id=str(t.get("id")),
                        name=t.get("name") or f"team {t.get('id')}",
                        mine=mine, players=players))
    return out


_ESPN_POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}


# ---------------------------------------------------------------------------
# Yahoo
# ---------------------------------------------------------------------------
def _yahoo(league) -> list[Team]:
    from . import yahoo
    key = league.raw.get("league_key") or f"nfl.l.{league.league_id}"
    d = yahoo.get(f"/league/{key}/teams/roster")
    node = d["fantasy_content"]["league"][1]["teams"]
    out = []
    for k, v in node.items():
        if not k.isdigit():
            continue
        team = v["team"]
        meta = team[0] if isinstance(team, list) else team
        info = {}
        for bit in (meta if isinstance(meta, list) else []):
            if isinstance(bit, dict):
                info.update(bit)
        mine = str(info.get("is_owned_by_current_login", "0")) == "1"
        players = []
        roster = next((p for p in team[1:] if isinstance(p, dict) and "roster" in p), None)
        if roster:
            pl = roster["roster"]
            pl = pl.get("0", pl) if isinstance(pl, dict) else pl
            plist = (pl or {}).get("players") if isinstance(pl, dict) else None
            for pk, pv in (plist or {}).items():
                if not pk.isdigit():
                    continue
                bits = pv["player"][0]
                rec = {}
                for b in bits:
                    if isinstance(b, dict):
                        rec.update(b)
                nm = (rec.get("name") or {}).get("full")
                if nm:
                    players.append({
                        "name": nm,
                        "position": rec.get("display_position"),
                        "team": rec.get("editorial_team_abbr")})
        out.append(Team(team_id=str(info.get("team_id")),
                        name=info.get("name") or f"team {info.get('team_id')}",
                        mine=mine, players=players))
    return out


# ---------------------------------------------------------------------------
def all_teams(league, week: int, players_blob: dict | None = None) -> list[Team]:
    if league.platform == "sleeper":
        return _sleeper(league, players_blob or {})
    if league.platform == "espn":
        return _espn(league, week)
    if league.platform == "yahoo":
        return _yahoo(league)
    raise RuntimeError(f"no roster reader for {league.platform}")


def opponent_of(league, week: int, teams: list[Team]) -> Team | None:
    """This week's opponent, or None in a league with no head-to-head matchup."""
    if league.raw.get("guillotine"):
        return None                       # everyone plays the field, not a rival
    if league.platform == "sleeper":
        oid = _sleeper_opponent(league, week, teams)
        return next((t for t in teams if t.team_id == oid), None)
    return None                           # ESPN/Yahoo matchups: not needed yet
