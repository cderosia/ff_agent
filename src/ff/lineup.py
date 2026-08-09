"""Weekly projections, availability, and lineup optimisation.

Season totals are the right input for a draft and the wrong one for a Sunday.
In-season we use each provider's WEEKLY projection, which already carries the
things you'd otherwise try to model yourself -- opponent, depth chart, and
injury news -- refreshed continuously by the providers.

What the providers do NOT reliably do is stop you starting someone who is out
or on bye. That is where real points are lost, and it is fully automatable, so
it happens here.
"""
from __future__ import annotations

import json
import pathlib
import time

import pandas as pd
import requests

from .names import key
from .stats import ESPN_STAT, SLEEPER_STAT, SLOT_ELIGIBILITY, score

ROOT = pathlib.Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "raw"
SEASON = 2026
UA = {"User-Agent": "Mozilla/5.0"}
ESPN_URL = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{SEASON}"
            f"/segments/0/leaguedefaults/3?view=kona_player_info")

OUT_STATUSES = {"Out", "IR", "PUP", "Suspended", "NA", "Doubtful", "DNR"}
RISK_STATUSES = {"Questionable"}


# ---------------------------------------------------------------------------
# weekly projections
# ---------------------------------------------------------------------------
def espn_weekly(week: int, limit: int = 600, max_age_hours: int = 6,
                force: bool = False) -> dict:
    """{(name,pos): stat_line} from ESPN's week-specific projection."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"espn_proj_{SEASON}_raw.json"
    if path.exists() and not force and \
            (time.time() - path.stat().st_mtime) / 3600 < max_age_hours:
        payload = json.loads(path.read_text())
    else:
        hdrs = dict(UA)
        hdrs["x-fantasy-filter"] = json.dumps({"players": {
            "limit": limit,
            "sortDraftRanks": {"sortPriority": 1, "sortAsc": True, "value": "PPR"}}})
        r = requests.get(ESPN_URL, headers=hdrs, timeout=60)
        r.raise_for_status()
        payload = r.json()
        path.write_text(json.dumps(payload))

    POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE"}
    out = {}
    for e in payload.get("players", []):
        p = e["player"]
        pos = POS.get(p.get("defaultPositionId"))
        if not pos:
            continue
        s = next((x for x in p.get("stats", [])
                  if x.get("seasonId") == SEASON and x.get("statSourceId") == 1
                  and x.get("statSplitTypeId") == 1
                  and x.get("scoringPeriodId") == week), None)
        if not s:
            continue
        line = {}
        for sid, val in (s.get("stats") or {}).items():
            nm = ESPN_STAT.get(int(sid))
            if nm and val:
                line[nm] = float(val)
        if line:
            out[key(p["fullName"], pos)] = line
    return out


def sleeper_weekly(week: int, max_age_hours: int = 6, force: bool = False) -> dict:
    """{(name,pos): stat_line} from Sleeper/Rotowire's week projection."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"sleeper_proj_{SEASON}_w{week}.json"
    if path.exists() and not force and \
            (time.time() - path.stat().st_mtime) / 3600 < max_age_hours:
        rows = json.loads(path.read_text())
    else:
        url = (f"https://api.sleeper.com/projections/nfl/{SEASON}/{week}"
               f"?season_type=regular&position[]=QB&position[]=RB"
               f"&position[]=WR&position[]=TE&order_by=pts_ppr")
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        rows = r.json()
        path.write_text(json.dumps(rows))

    out = {}
    for row in rows:
        pl = row.get("player") or {}
        pos = pl.get("position")
        if pos not in ("QB", "RB", "WR", "TE"):
            continue
        line = {}
        for k, v in (row.get("stats") or {}).items():
            nm = SLEEPER_STAT.get(k)
            if nm and v:
                line[nm] = float(v)
        if line:
            nm_full = f"{pl.get('first_name','')} {pl.get('last_name','')}".strip()
            out[key(nm_full, pos)] = line
    return out


def weekly_points(week: int, scoring: dict) -> dict:
    """{(name,pos): (points, n_sources, spread)} blended for one week."""
    a, b = espn_weekly(week), sleeper_weekly(week)
    out = {}
    for k in set(a) | set(b):
        lines = [x for x in (a.get(k), b.get(k)) if x]
        pts = [score(line, scoring) for line in lines]
        out[k] = (sum(pts) / len(pts), len(pts),
                  (max(pts) - min(pts)) if len(pts) > 1 else 0.0)
    return out


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------
def schedule(season: int = SEASON) -> pd.DataFrame:
    path = CACHE / "nflverse_games.csv"
    if not path.exists():
        r = requests.get(
            "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv",
            timeout=120)
        r.raise_for_status()
        path.write_bytes(r.content)
    d = pd.read_csv(path, low_memory=False)
    return d[d.season == season]


def bye_teams(week: int, season: int = SEASON) -> set:
    """Teams NOT playing in a given week."""
    s = schedule(season)
    playing = set(s[s.week == week].home_team) | set(s[s.week == week].away_team)
    allteams = set(s.home_team) | set(s.away_team)
    return allteams - playing


def outdoor_games(week: int, season: int = SEASON) -> dict:
    """{team: True} for teams playing outdoors this week."""
    s = schedule(season)
    wk = s[s.week == week]
    out = {}
    for _, g in wk.iterrows():
        outdoor = str(g.get("roof", "")).lower() == "outdoors"
        out[g.home_team] = outdoor
        out[g.away_team] = outdoor
    return out


_INJURY_INDEX: dict = {}


def injury_index(blob: dict) -> dict:
    """{(name,pos): (status, body_part)} built once -- the blob is 12k players
    and a linear scan per lookup is far too slow across five rosters."""
    global _INJURY_INDEX
    if _INJURY_INDEX:
        return _INJURY_INDEX
    idx = {}
    for p in blob.values():
        pos = p.get("position")
        if pos not in ("QB", "RB", "WR", "TE"):
            continue
        nm = f"{p.get('first_name','')} {p.get('last_name','')}".strip()
        if nm:
            idx[key(nm, pos)] = (p.get("injury_status"), p.get("injury_body_part"))
    _INJURY_INDEX = idx
    return idx


def availability(blob: dict, name: str, pos: str, team: str | None,
                 week: int, projected: float | None = None) -> tuple[str, str]:
    """('ok'|'risk'|'out'|'unknown', reason) for one player.

    A player on bye projects 0.0, which is correct but looks identical to a
    missing projection. Distinguish them explicitly -- an unexplained 0.0 reads
    as a broken model and will get second-guessed on a Sunday.
    """
    if team and team in bye_teams(week):
        return "out", f"bye week ({team})"
    st, part = injury_index(blob).get(key(name, pos), (None, None))
    if st in OUT_STATUSES:
        return "out", st + (f" ({part})" if part else "")
    if st in RISK_STATUSES:
        return "risk", st + (f" ({part})" if part else "")
    if projected is not None and projected <= 0:
        return "unknown", "no projection for this week"
    return "ok", ""


# ---------------------------------------------------------------------------
# optimiser
# ---------------------------------------------------------------------------
def optimize(players: list[dict], league) -> tuple[dict, list[dict]]:
    """Best legal lineup. Returns ({slot: [players]}, bench).

    Greedy by projected points, filling dedicated slots before flex. With the
    slot counts these leagues use, greedy is optimal: a player who is best at a
    dedicated slot can never be better used in a flex that a lesser player of
    the same position could fill instead.
    """
    open_slots = dict(league.starters)
    dedicated = [s for s in open_slots if len(SLOT_ELIGIBILITY.get(s, {s})) == 1]
    flexes = [s for s in open_slots if len(SLOT_ELIGIBILITY.get(s, {s})) > 1]
    filled = {s: [] for s in open_slots}
    used = set()

    for p in sorted(players, key=lambda x: -x.get("week_points", 0)):
        if p.get("status") in ("out", "unknown"):
            continue
        for slot in dedicated + flexes:
            if open_slots.get(slot, 0) > 0 and \
                    p["position"] in SLOT_ELIGIBILITY.get(slot, {slot}):
                open_slots[slot] -= 1
                filled[slot].append(p)
                used.add(id(p))
                break
    bench = [p for p in players if id(p) not in used]
    return filled, bench


def close_calls(filled: dict, bench: list[dict], league,
                margin: float = 1.5) -> list[dict]:
    """Starter/bench pairs within `margin` points — the decisions worth a look."""
    out = []
    for slot, starters in filled.items():
        elig = SLOT_ELIGIBILITY.get(slot, {slot})
        alts = [b for b in bench
                if b["position"] in elig and b.get("status") != "out"]
        for s in starters:
            for a in alts:
                gap = s.get("week_points", 0) - a.get("week_points", 0)
                if 0 <= gap <= margin:
                    out.append({"slot": slot, "starting": s, "alternative": a,
                                "gap": round(gap, 1)})
    out.sort(key=lambda x: x["gap"])
    return out
