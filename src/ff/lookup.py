"""One player's raw projected stat lines, source by source.

Every number the app shows is points, and points hide the reasoning: 14.2
could be 80 yards and a touchdown or 7 catches and 60. This is the layer
underneath -- the stat lines each source actually published, the blend the
app scores, and what has really happened -- so a projection can be checked
rather than taken on trust.

Nothing here is a new model. The blends are the SAME ones the rest of the app
scores: the week blend is the mean line with a missing stat counted as zero
(exactly what `lineup.weekly_points` does, since it averages points and
scoring is linear), and the season blend is the board's own `stats` from
`projections.fetch`.

ESPN is fetched for the ONE player, by id, on a short cache. That is what lets
the page move during a game: the full projection payload is 20MB and cached
for an hour, while a single player is ~0.25s.
"""
from __future__ import annotations

import functools
import json
import time

import requests

from .lineup import ESPN_URL, SEASON, UA, espn_weekly, sleeper_weekly
from .names import key
from .stats import ESPN_STAT, fold_fg_buckets, score

# Display order. Anything a source publishes that is not listed here is
# appended after these, so nothing is silently dropped from the table.
ORDER = [
    "pass_att", "pass_cmp", "pass_yds", "pass_td", "pass_int", "pass_2pt",
    "rush_att", "rush_yds", "rush_td", "rush_2pt",
    "targets", "receptions", "rec_yds", "rec_td", "rec_2pt",
    "fumbles", "fum_lost",
    "fg_made_0_19", "fg_made_20_29", "fg_made_30_39", "fg_made_0_39",
    "fg_made_40_49", "fg_made_50", "fg_made_50_59", "fg_made_60",
    "fg_missed", "xp_made", "xp_missed",
]

LABEL = {
    "pass_att": "Pass att", "pass_cmp": "Completions", "pass_yds": "Pass yds",
    "pass_td": "Pass TD", "pass_int": "Interceptions", "pass_2pt": "Pass 2pt",
    "rush_att": "Carries", "rush_yds": "Rush yds", "rush_td": "Rush TD",
    "rush_2pt": "Rush 2pt", "targets": "Targets", "rec_target": "Targets",
    "receptions": "Receptions", "rec_yds": "Rec yds", "rec_td": "Rec TD",
    "rec_2pt": "Rec 2pt", "fumbles": "Fumbles", "fum_lost": "Fumbles lost",
    "fg_made_0_19": "FG 0-19", "fg_made_20_29": "FG 20-29",
    "fg_made_30_39": "FG 30-39", "fg_made_0_39": "FG 0-39",
    "fg_made_40_49": "FG 40-49", "fg_made_50": "FG 50+",
    "fg_made_50_59": "FG 50-59", "fg_made_60": "FG 60+",
    "fg_missed": "FG missed", "xp_made": "XP made", "xp_missed": "XP missed",
    "fg_att": "FG att", "fg_made_total": "FG made", "fg_yds": "FG yds (made)",
}


def _espn_line(stats: dict | None) -> dict:
    out: dict = {}
    for sid, val in (stats or {}).items():
        nm = ESPN_STAT.get(int(sid))
        # stats.ESPN_STAT maps id 41 to "targets", but in ESPN's actual lines
        # it equals receptions (Chase, weeks 1-4: 21 and 21, against 28 real
        # targets in id 58 -> rec_target). Nothing scores it; it is left out
        # here rather than fixed there, because projlog stores that column.
        if nm == "targets":
            continue
        if nm and val:
            # Two ESPN ids can map to one canonical stat (targets); keep one.
            out.setdefault(nm, float(val))
    return out


def _sum(lines: list[dict]) -> dict:
    out: dict = {}
    for ln in lines:
        for k, v in ln.items():
            out[k] = out.get(k, 0.0) + v
    return out


def shown_blend(lines: list[dict]) -> dict:
    """Mean over only the sources that PUBLISHED each stat -- for display.

    `blend` counts a missing stat as zero, which is right for SCORING (it is
    what the app scores) and wrong for reading: Sleeper publishes no targets,
    so Chase's blended targets came out 5.25 against ESPN's 10.5. The table
    shows this; the points row is still scored from `blend`.
    """
    out: dict = {}
    for ln in lines:
        for k, v in (ln or {}).items():
            out.setdefault(k, []).append(v)
    return {k: sum(v) / len(v) for k, v in out.items()}


def blend(lines: list[dict]) -> dict:
    """Mean stat line, a missing stat counted as zero (see module docstring)."""
    lines = [ln for ln in lines if ln]
    if not lines:
        return {}
    keys = set().union(*lines)
    return {k: sum(ln.get(k, 0.0) for ln in lines) / len(lines) for k in keys}


@functools.lru_cache(maxsize=4)
def _espn_ids(_bucket: int) -> dict:
    """{(name,pos): espn_id} from the full cached payload. The board only
    carries ids for the players ESPN's season feed returned; the weekly
    payload covers more."""
    from .lineup import CACHE
    path = CACHE / f"espn_proj_{SEASON}_raw.json"
    POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {}
    out = {}
    for e in payload.get("players", []):
        p = e["player"]
        pos = POS.get(p.get("defaultPositionId"))
        if pos and p.get("id"):
            out[key(p.get("fullName", ""), pos)] = int(p["id"])
    return out


def espn_id_for(name: str, pos: str, known: int | None = None) -> int | None:
    return known or _espn_ids(int(time.time() // 3600)).get(key(name, pos))


@functools.lru_cache(maxsize=256)
def _espn_player(espn_id: int, _bucket: int) -> dict:
    h = dict(UA)
    h["x-fantasy-filter"] = json.dumps(
        {"players": {"filterIds": {"value": [int(espn_id)]}}})
    r = requests.get(ESPN_URL, headers=h, timeout=30)
    r.raise_for_status()
    ps = r.json().get("players") or []
    return ps[0]["player"] if ps else {}


def espn_player(espn_id: int, ttl: int = 60) -> dict:
    """One ESPN player record, re-fetched at most every `ttl` seconds."""
    return _espn_player(int(espn_id), int(time.time() // max(1, ttl)))


def espn_split(player: dict, week: int) -> dict:
    """ESPN's lines for one player, split around `week`.

    statSourceId 0 = actual, 1 = projected; statSplitTypeId 1 = one week,
    0 = season total. ESPN can publish an empty stub beside a real row for
    the same week (see lineup.espn_weekly), so the richest row wins.
    """
    proj, act, act_season = {}, {}, {}
    for s in player.get("stats") or []:
        if s.get("seasonId") != SEASON:
            continue
        src, split, wk = (s.get("statSourceId"), s.get("statSplitTypeId"),
                          s.get("scoringPeriodId"))
        line = _espn_line(s.get("stats"))
        if split == 1:
            bucket = proj if src == 1 else act if src == 0 else None
            if bucket is not None and len(line) >= len(bucket.get(wk) or {}):
                bucket[wk] = line
        elif split == 0 and src == 0:
            act_season = line
    return {
        "week_proj": proj.get(week) or {},
        "week_actual": act.get(week) or {},
        # Actual weeks BEFORE this one, and projected weeks from this one on,
        # so a live game is never counted twice.
        "to_date": _sum([v for w, v in act.items() if w < week]),
        "ros": _sum([v for w, v in proj.items() if w >= week]),
        "season_actual": act_season,
        "weeks_played": sorted(w for w, v in act.items() if v and w < week),
    }


def points(line: dict, scoring: dict, pos: str) -> float:
    if not line:
        return 0.0
    if pos == "K":
        line = fold_fg_buckets(line, scoring)
    return score(line, scoring)


def player(rec: dict, week: int, live_ttl: int = 60) -> dict:
    """Everything the lookup page shows for one board record.

    `rec` is a `projections.fetch()` record: name, position, team, espn_id,
    `source_lines` (each feed's SEASON line) and `stats` (the board blend).
    """
    name, pos = rec["name"], rec["position"]
    k = key(name, pos)
    out = {"name": name, "position": pos, "team": rec.get("team"),
           # Per-source REST-OF-SEASON lines and the 17-game pace (ff.ros).
           "season_sources": dict(rec.get("source_lines") or {}),
           "season_blend": dict(rec.get("stats") or {}),
           "errors": []}

    esp = {}
    eid = espn_id_for(name, pos, rec.get("espn_id"))
    if eid:
        try:
            esp = espn_split(espn_player(eid, live_ttl), week)
        except Exception as e:
            out["errors"].append(f"ESPN live: {type(e).__name__}")
    # ESPN publishes kicker field goals only as distance BUCKETS, which score
    # near zero in a league paying by distance; `lineup.espn_weekly` leaves
    # kickers out for that reason, and so does this.
    espn_week = {} if pos == "K" else (
        esp.get("week_proj") or espn_weekly(week).get(k) or {})
    try:
        slp_week = sleeper_weekly(week).get(k) or {}
    except Exception as e:
        slp_week = {}
        out["errors"].append(f"Sleeper weekly: {type(e).__name__}")

    out["week"] = {"ESPN": espn_week, "Sleeper": slp_week,
                   "Blend": blend([espn_week, slp_week])}
    out["week_shown"] = shown_blend([espn_week, slp_week])
    # Kicker ACTUALS are ESPN buckets too, so they go the same way: Brandon
    # Aubrey scored 12.0 over four games from them in friends.
    k_ok = pos != "K"
    out["actual_week"] = (esp.get("week_actual") or {}) if k_ok else {}
    out["to_date"] = (esp.get("to_date") or {}) if k_ok else {}
    out["ros"] = (esp.get("ros") or {}) if k_ok else {}
    out["weeks_played"] = (esp.get("weeks_played") or []) if k_ok else []
    out["espn_id"] = eid
    return out


def ordered(stat_keys) -> list[str]:
    ks = [k for k in ORDER if k in stat_keys]
    return ks + sorted(k for k in stat_keys if k not in ORDER)
