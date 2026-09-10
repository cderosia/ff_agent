"""Every team's roster, from any platform, in one shape.

Win probability needs more than your own team: a head-to-head week needs your
opponent, and a guillotine week needs all sixteen. Each platform answers that
differently, so the difference is absorbed here and nothing downstream has to
know which site a league lives on.

Returns a list of Team records. `mine` marks yours; `opponent_of` resolves the
week's matchup where the league has one.
"""
from __future__ import annotations

import functools
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
    # What the platform says you are ACTUALLY starting, slot by slot. Without
    # this the app could only ever show its own optimal lineup and had no way to
    # notice that the lineup you really set still has a doubtful player in it.
    starters: list = field(default_factory=list)  # [{name, position, slot}]


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
        # `starters` is a list of player ids positionally aligned with the
        # league's roster_positions, with "0" for an empty slot.
        slots = [x for x in (league.raw.get("roster_positions") or [])
                 if x not in ("BN", "IR", "TAXI")]
        started = []
        for i, pid in enumerate(r.get("starters") or []):
            if not pid or pid == "0":
                started.append({"name": None, "position": None,
                                "slot": slots[i] if i < len(slots) else "?"})
                continue
            pl = blob.get(str(pid)) or {}
            nm = pl.get("full_name") or " ".join(
                x for x in (pl.get("first_name"), pl.get("last_name")) if x)
            started.append({"name": nm or str(pid),
                            "position": pl.get("position") or (
                                "DST" if str(pid).isalpha() else None),
                            # pro team, so kickoff state is resolvable
                            "team": pl.get("team") or (
                                str(pid).upper() if str(pid).isalpha() else None),
                            "slot": slots[i] if i < len(slots) else "?"})
        out.append(Team(team_id=str(r.get("roster_id")),
                        name=(u.get("metadata") or {}).get("team_name")
                             or u.get("display_name") or f"team {r.get('roster_id')}",
                        mine=bool(me and owner == me), players=players,
                        starters=started))
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
def _espn(league, week, blob=None) -> list[Team]:
    ck = {"espn_s2": _env("ESPN_S2"), "SWID": _env("ESPN_SWID")}
    url = (f"{ESPN}/seasons/2026/segments/0/leagues/{league.league_id}"
           f"?view=mRoster&view=mTeam")
    d = requests.get(url, headers=UA, cookies=ck, timeout=40).json()
    swid = (_env("ESPN_SWID") or "").strip("{}").lower()
    out = []
    for t in d.get("teams") or []:
        mine = any(str(o).strip("{}").lower() == swid for o in (t.get("owners") or []))
        players, started = [], []
        for e in ((t.get("roster") or {}).get("entries") or []):
            pp = ((e.get("playerPoolEntry") or {}).get("player") or {})
            nm = pp.get("fullName")
            if not nm:
                continue
            pos = _ESPN_POS.get(pp.get("defaultPositionId"))
            # ESPN's roster payload carries no pro team. Left as None it made
            # every ESPN player look like he was on bye and, worse, zeroed the
            # defense -- whose weekly points are looked up BY TEAM. Resolve it
            # here so every consumer (lineups, win probability, the dashboard)
            # gets it, rather than each patching around the gap.
            from .lineup import _team_from_blob
            team = _team_from_blob(blob or {}, nm, pos) if blob else None
            players.append({"name": nm, "position": pos, "team": team})
            slot_id = e.get("lineupSlotId")
            if slot_id is not None and slot_id not in (20, 21):   # bench / IR
                started.append({"name": nm, "position": pos, "team": team,
                                "slot": _ESPN_SLOT_NAME.get(slot_id, str(slot_id))})
        out.append(Team(team_id=str(t.get("id")),
                        name=t.get("name") or f"team {t.get('id')}",
                        mine=mine, players=players, starters=started))
    return out


_ESPN_SLOT_NAME = {0: "QB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE",
                   7: "SUPERFLEX", 16: "DST", 17: "K", 23: "FLEX"}


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
        players, started = [], []
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
                if not nm:
                    continue
                pos = rec.get("display_position")
                players.append({"name": nm, "position": pos,
                                "team": rec.get("editorial_team_abbr")})
                # Yahoo puts the slot you actually started him in under
                # `selected_position`, alongside the player node rather than
                # inside it. BN and IR are not starting slots.
                slot = None
                for part in pv["player"][1:]:
                    if isinstance(part, dict) and "selected_position" in part:
                        for bit in part["selected_position"]:
                            if isinstance(bit, dict) and "position" in bit:
                                slot = bit["position"]
                if slot and slot not in ("BN", "IR", "IL"):
                    started.append({"name": nm, "position": pos, "slot": slot,
                                    "team": rec.get("editorial_team_abbr")})
        out.append(Team(team_id=str(info.get("team_id")),
                        name=info.get("name") or f"team {info.get('team_id')}",
                        mine=mine, players=players, starters=started))
    return out


# ---------------------------------------------------------------------------
def all_teams(league, week: int, players_blob: dict | None = None) -> list[Team]:
    if league.platform == "sleeper":
        return _sleeper(league, players_blob or {})
    if league.platform == "espn":
        return _espn(league, week, players_blob or {})
    if league.platform == "yahoo":
        return _yahoo(league)
    raise RuntimeError(f"no roster reader for {league.platform}")


@functools.lru_cache(maxsize=8)
def _espn_schedule(league_id: str) -> tuple:
    """((matchupPeriodId, awayTeamId, homeTeamId), ...) for the whole season.

    `mMatchup` is the only view that carries the schedule -- `mSchedule` returns
    nothing -- and it weighs ~800KB because it embeds every roster. Cached, and
    reduced to three ints per game the moment it lands.
    """
    ck = {"espn_s2": _env("ESPN_S2"), "SWID": _env("ESPN_SWID")}
    url = (f"{ESPN}/seasons/2026/segments/0/leagues/{league_id}?view=mMatchup")
    d = requests.get(url, headers=UA, cookies=ck, timeout=60).json()
    out = []
    for m in (d.get("schedule") or []):
        a = (m.get("away") or {}).get("teamId")
        h = (m.get("home") or {}).get("teamId")
        if a is not None and h is not None:
            out.append((m.get("matchupPeriodId"), a, h))
    return tuple(out)


def _espn_opponent(league, week: int, teams: list[Team]) -> str | None:
    mine = next((t for t in teams if t.mine), None)
    if not mine:
        return None
    try:
        sched = _espn_schedule(str(league.league_id))
    except Exception:
        return None
    me = int(mine.team_id)
    for period, away, home in sched:
        if period != week:
            continue
        if away == me:
            return str(home)
        if home == me:
            return str(away)
    return None


def opponent_of(league, week: int, teams: list[Team]) -> Team | None:
    """This week's opponent, or None in a league with no head-to-head matchup."""
    if league.raw.get("guillotine"):
        return None                       # everyone plays the field, not a rival
    if league.platform == "sleeper":
        oid = _sleeper_opponent(league, week, teams)
    elif league.platform == "espn":
        oid = _espn_opponent(league, week, teams)
    else:
        return None                       # Yahoo here is the guillotine league
    return next((t for t in teams if t.team_id == oid), None)


def has_drafted(league, teams: list[Team]) -> bool:
    """Whether this league's CURRENT season has actually drafted.

    Roster contents alone can't answer it. A keeper league carries last
    season's rosters straight through the offseason, so `freinds-keeper` looks
    fully rostered in August while its 2026 draft hasn't happened -- and odds
    computed off those rosters describe a team that no longer exists.
    """
    status = (league.raw.get("status") or league.raw.get("draft_status") or "")
    if str(status).lower() in ("pre_draft", "predraft", "predraftready"):
        return False
    return any(t.players for t in teams)


def board_rosters(league, by_key: dict, week: int = 1, blob: dict | None = None
                  ) -> tuple[list, list]:
    """Every team's roster as BOARD ROWS. Returns (mine, [other teams]).

    Drop-in replacement for the Sleeper-only trades.league_rosters, which is
    why the Lineup, Waivers and Trades tabs only ever worked on one platform.
    Players with no board row -- kickers, defenses, anyone missing from the
    projections -- are dropped, exactly as before.
    """
    from .names import key as nkey

    teams = all_teams(league, week, blob)
    mine, others = [], []
    for t in teams:
        rows = []
        for p in t.players:
            r = by_key.get(nkey(p["name"], p.get("position")))
            if r:
                rows.append(r)
        if t.mine:
            mine = rows
        else:
            others.append({"roster_id": t.team_id, "owner": t.team_id,
                           "name": t.name, "players": rows})
    return mine, others


def all_matchups(league, week: int, teams: list[Team]) -> list[tuple]:
    """Every game in the league this week, as (home_id, away_id) pairs.

    Empty for the guillotine league, which has no matchups at all -- there the
    whole field is your opponent and the only question is who finishes last.
    """
    if league.raw.get("guillotine"):
        return []
    if league.platform == "sleeper":
        try:
            ms = requests.get(
                f"{SLEEPER}/league/{league.league_id}/matchups/{week}",
                timeout=30).json() or []
        except Exception:
            return []
        by_mid: dict = {}
        for m in ms:
            by_mid.setdefault(m.get("matchup_id"), []).append(str(m.get("roster_id")))
        return [tuple(v[:2]) for v in by_mid.values() if len(v) >= 2]
    if league.platform == "espn":
        try:
            sched = _espn_schedule(str(league.league_id))
        except Exception:
            return []
        return [(str(h), str(a)) for period, a, h in sched if period == week]
    return []
