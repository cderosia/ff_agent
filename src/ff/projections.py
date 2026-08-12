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
import re
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

# ESPN proTeamId -> abbreviation, so bye weeks can be resolved from the schedule
ESPN_TEAM = {
    1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN", 8: "DET",
    9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LA", 15: "MIA",
    16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI",
    23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WAS", 29: "CAR",
    30: "JAX", 33: "BAL", 34: "HOU",
}


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
            "team": ESPN_TEAM.get(p.get("proTeamId")),
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
# FFToday projections
#
# The third opinion. ESPN is in-house and Sleeper is Rotowire; with only those
# two, a disagreement tells you THAT they disagree but not who is right, which
# makes the spread column a coin flip. FFToday is an independent human-produced
# set, so it breaks ties and turns spread into a usable confidence signal.
# ---------------------------------------------------------------------------
FFTODAY = ("https://www.fftoday.com/rankings/playerproj.php"
           "?Season={yr}&PosID={pid}&LeagueID=1&cur_page={pg}")

# The table's stat columns, in page order, per position. Every row is
# [blank, player, team, bye, *these, fantasy_points].
FFTODAY_COLS = {
    ("QB", 10): ["pass_cmp", "pass_att", "pass_yds", "pass_td", "pass_int",
                 "rush_att", "rush_yds", "rush_td"],
    ("RB", 20): ["rush_att", "rush_yds", "rush_td",
                 "receptions", "rec_yds", "rec_td"],
    ("WR", 30): ["receptions", "rec_yds", "rec_td",
                 "rush_att", "rush_yds", "rush_td"],
    ("TE", 40): ["receptions", "rec_yds", "rec_td"],
}

_TAGS = re.compile(r"<[^>]+>")
_CELL = re.compile(r"<TD[^>]*>(.*?)</TD>", re.S | re.I)
_LINK = re.compile(r"/stats/players/\d+/", re.I)


def _text(cell: str) -> str:
    return _TAGS.sub("", cell).replace("&nbsp;", " ").replace(",", "").strip()


def fetch_fftoday(max_age_hours: int = 12, force: bool = False) -> list[dict]:
    """FFToday season projections, scraped into the same canonical shape."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / f"fftoday_proj_{SEASON}.json"
    if cached.exists() and not force:
        if (time.time() - cached.stat().st_mtime) / 3600 < max_age_hours:
            return json.loads(cached.read_text())

    out = []
    for (pos, pid), cols in FFTODAY_COLS.items():
        for page in range(4):                      # paginates 50/page
            r = requests.get(FFTODAY.format(yr=SEASON, pid=pid, pg=page),
                             headers=UA, timeout=45)
            r.raise_for_status()
            found = 0
            for chunk in re.split(r"<TR[ >]", r.text, flags=re.I)[1:]:
                if not _LINK.search(chunk):
                    continue
                cells = [_text(c) for c in _CELL.findall(chunk)]
                # [blank, name, team, bye, *stats, points]
                if len(cells) < 4 + len(cols) + 1:
                    continue
                found += 1
                line = {}
                for name, raw in zip(cols, cells[4:4 + len(cols)]):
                    try:
                        v = float(raw)
                    except ValueError:
                        continue
                    if v:
                        line[name] = v
                if not line:
                    continue
                out.append({"name": cells[1], "position": pos,
                            "team": cells[2] or None, "stats": line})
            if found < 50:                         # last page for this position
                break

    cached.write_text(json.dumps(out))
    return out


# ---------------------------------------------------------------------------
# Blend
# ---------------------------------------------------------------------------
# Three is the cap on purpose. Two cannot break a tie; the third is what makes
# `spread` mean something. Past three the marginal source moves the top-100 board
# by only a rank or two, so it buys noise and another thing that can break.
MAX_SOURCES = 3

FETCHERS = {"espn": fetch_espn, "sleeper": fetch_sleeper, "fftoday": fetch_fftoday}


def fetch(force: bool = False,
          sources: tuple = ("espn", "sleeper", "fftoday")) -> list[dict]:
    """Blended projections. Averages each stat across whichever sources have it.

    A source that is unreachable is skipped rather than fatal -- a scraped source
    WILL break eventually, and a draft board from two sources beats no board.
    """
    sources = tuple(sources)[:MAX_SOURCES]

    pulled: dict[str, list] = {}
    for src in sources:
        try:
            pulled[src] = FETCHERS[src](force=force)
        except Exception as e:
            print(f"  ! projection source '{src}' failed, continuing without it: {e}")

    merged: dict = {}
    for src in sources:
        for p in pulled.get(src, []):
            k = key(p["name"], p["position"])
            rec = merged.get(k)
            if rec is None:
                rec = merged[k] = {
                    "espn_id": None, "name": p["name"], "position": p["position"],
                    "pro_team_id": None, "espn_points": 0.0, "adp": 0.0,
                    "auction_value": 0.0, "pct_owned": 0.0, "games": None,
                    "team": p.get("team"), "lines": {}, "sleeper_adp": None,
                }
            rec["lines"][src] = p["stats"]
            if p.get("team"):
                rec["team"] = p["team"]
            # Extras are per-source and NOT interchangeable: ESPN's `adp` is a
            # float, Sleeper's is a dict keyed by scoring format. Carrying them
            # generically would put a dict in `adp` and break the market join.
            if src == "espn":
                for f in ("espn_id", "pro_team_id", "espn_points", "adp",
                          "auction_value", "pct_owned"):
                    if p.get(f):
                        rec[f] = p[f]
            elif src == "sleeper":
                rec["sleeper_adp"] = p.get("adp")
                if p.get("games"):
                    rec["games"] = p["games"]

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
