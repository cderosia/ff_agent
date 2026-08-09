"""2026 season projections, as raw per-stat lines.

We deliberately keep the *raw* stat line rather than a points total: each league
scores the same projection differently, so points are computed per league later.

Two independent sources are blended:
  ESPN    - ESPN's in-house projections
  Sleeper - served by Sleeper, produced by Rotowire (`company: rotowire`)

Blending happens at the STAT level, not the points level, so the result can still
be rescored under any league's rules. A single source is a single point of
failure; disagreement between the two is also a useful uncertainty signal.
"""
from __future__ import annotations

import json
import pathlib
import time

import requests

from .names import key
from .stats import ESPN_STAT, SLEEPER_STAT

ROOT = pathlib.Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "raw"
SEASON = 2026
URL = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{SEASON}"
       f"/segments/0/leaguedefaults/3?view=kona_player_info")
UA = {"User-Agent": "Mozilla/5.0"}

POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}


def fetch_espn(limit: int = 600, max_age_hours: int = 12, force: bool = False) -> list[dict]:
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
        own = p.get("ownership") or {}
        out.append({
            "espn_id": p.get("id"),
            "name": p.get("fullName"),
            "position": pos,
            "pro_team_id": p.get("proTeamId"),
            "espn_points": round(season.get("appliedTotal") or 0.0, 2),
            # ESPN's own market price — the real ADP in ESPN leagues
            "adp": own.get("averageDraftPosition") or 0.0,
            "auction_value": own.get("auctionValueAverage") or 0.0,
            "pct_owned": round(own.get("percentOwned") or 0.0, 1),
            "stats": line,
        })

    cached.write_text(json.dumps(out))
    return out


# ---------------------------------------------------------------------------
# Sleeper (Rotowire) projections
# ---------------------------------------------------------------------------
SLEEPER_URL = (
    "https://api.sleeper.com/projections/nfl/{yr}?season_type=regular"
    "&position[]=QB&position[]=RB&position[]=WR&position[]=TE&order_by=pts_ppr"
)

# Sleeper carries redraft ADP right on the projection payload.
SLEEPER_ADP_KEYS = {"ppr": "adp_ppr", "half-ppr": "adp_half_ppr", "standard": "adp_std"}


def fetch_sleeper(max_age_hours: int = 12, force: bool = False) -> list[dict]:
    """Sleeper/Rotowire season projections in the same canonical shape."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / f"sleeper_proj_{SEASON}.json"
    if cached.exists() and not force:
        if (time.time() - cached.stat().st_mtime) / 3600 < max_age_hours:
            return json.loads(cached.read_text())

    r = requests.get(SLEEPER_URL.format(yr=SEASON), timeout=90)
    r.raise_for_status()

    seen, out = set(), []
    for row in r.json():
        pl = row.get("player") or {}
        pid = row.get("player_id")
        pos = pl.get("position")
        if pid in seen or pos not in ("QB", "RB", "WR", "TE"):
            continue
        st = row.get("stats") or {}
        line = {}
        for k, v in st.items():
            name = SLEEPER_STAT.get(k)
            if name and v:
                line[name] = float(v)
        if not line:
            continue
        seen.add(pid)
        full = f"{pl.get('first_name','')} {pl.get('last_name','')}".strip()
        out.append({
            "sleeper_id": pid,
            "name": full,
            "position": pos,
            "team": row.get("team"),
            "games": st.get("gp"),
            "adp": {k: st.get(v) for k, v in SLEEPER_ADP_KEYS.items()},
            "stats": line,
        })

    cached.write_text(json.dumps(out))
    return out


# ---------------------------------------------------------------------------
# Blend
# ---------------------------------------------------------------------------
def fetch(force: bool = False, sources: tuple = ("espn", "sleeper")) -> list[dict]:
    """Blended projections. Averages each stat across whichever sources have it."""
    espn = fetch_espn(force=force) if "espn" in sources else []
    slp = fetch_sleeper(force=force) if "sleeper" in sources else []

    merged: dict = {}
    for p in espn:
        merged[key(p["name"], p["position"])] = {
            **p, "lines": {"espn": p["stats"]}, "sleeper_adp": None,
        }
    for p in slp:
        k = key(p["name"], p["position"])
        if k in merged:
            merged[k]["lines"]["sleeper"] = p["stats"]
            merged[k]["sleeper_adp"] = p["adp"]
            merged[k]["games"] = p.get("games")
        else:
            merged[k] = {
                "espn_id": None, "name": p["name"], "position": p["position"],
                "pro_team_id": None, "espn_points": 0.0, "adp": 0.0,
                "auction_value": 0.0, "pct_owned": 0.0, "games": p.get("games"),
                "lines": {"sleeper": p["stats"]}, "sleeper_adp": p["adp"],
            }

    out = []
    for k, rec in merged.items():
        lines = rec.pop("lines")
        stats: dict = {}
        for line in lines.values():
            for stat, val in line.items():
                stats.setdefault(stat, []).append(val)
        rec["stats"] = {s: sum(v) / len(v) for s, v in stats.items()}
        rec["sources"] = sorted(lines)
        rec["n_sources"] = len(lines)
        # per-source spread on the headline stat, as an uncertainty flag
        rec["source_lines"] = lines
        out.append(rec)
    return out
