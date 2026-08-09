"""2026 season projections, as raw per-stat lines.

We deliberately keep the *raw* stat line rather than a points total: each league
scores the same projection differently, so points are computed per league later.
"""
from __future__ import annotations

import json
import pathlib
import time

import requests

from .stats import ESPN_STAT

ROOT = pathlib.Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "raw"
SEASON = 2026
URL = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{SEASON}"
       f"/segments/0/leaguedefaults/3?view=kona_player_info")
UA = {"User-Agent": "Mozilla/5.0"}

POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}


def fetch(limit: int = 600, max_age_hours: int = 12, force: bool = False) -> list[dict]:
    """Player projections, cached to disk so drafting doesn't hammer ESPN."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / f"espn_proj_{SEASON}.json"

    if cached.exists() and not force:
        age = (time.time() - cached.stat().st_mtime) / 3600
        if age < max_age_hours:
            return json.loads(cached.read_text())

    hdrs = dict(UA)
    hdrs["x-fantasy-filter"] = json.dumps({
        "players": {
            "limit": limit,
            "sortDraftRanks": {"sortPriority": 1, "sortAsc": True, "value": "PPR"},
        }
    })
    r = requests.get(URL, headers=hdrs, timeout=60)
    r.raise_for_status()

    out = []
    for entry in r.json().get("players", []):
        p = entry["player"]
        pos = POS.get(p.get("defaultPositionId"))
        if pos is None:
            continue
        season = next(
            (s for s in p.get("stats", [])
             if s.get("seasonId") == SEASON
             and s.get("statSourceId") == 1        # 1 = projected
             and s.get("statSplitTypeId") == 0),   # 0 = full season
            None,
        )
        if not season:
            continue
        raw = season.get("stats") or {}
        line = {}
        for sid, val in raw.items():
            name = ESPN_STAT.get(int(sid))
            if name and val:
                line[name] = float(val)
        if not line:
            continue
        out.append({
            "espn_id": p.get("id"),
            "name": p.get("fullName"),
            "position": pos,
            "pro_team_id": p.get("proTeamId"),
            "espn_points": round(season.get("appliedTotal") or 0.0, 2),
            "stats": line,
        })

    cached.write_text(json.dumps(out))
    return out
