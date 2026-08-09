"""Market prices — what the room will actually pay for a player.

The board's value column is VBD rank (what a player is worth to YOU) against ADP
(what the field will make you pay). Which market to use depends on the platform:

  ESPN leagues  -> ESPN's own ADP, straight from the same feed as projections.
                   This is the real price in the room you're drafting in.
  Sleeper       -> Sleeper publishes no ADP, so fall back to FantasyFootballCalculator,
                   matched on scoring format.

CAVEAT (verified 2026-08-09): FFC's `teams` parameter is ignored -- 8-team and
14-team requests return byte-identical data. The scoring format param IS real
(189 of 201 players differ between standard and PPR), so we match on that only.
Do not claim team-count-matched ADP; it isn't.
"""
from __future__ import annotations

import json
import pathlib
import time

import requests

from .names import key

ROOT = pathlib.Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "raw"
SEASON = 2026
FFC = "https://fantasyfootballcalculator.com/api/v1/adp/{fmt}?teams=12&year={yr}&position=all"


def _cached(path: pathlib.Path, max_age_hours: float):
    if path.exists() and (time.time() - path.stat().st_mtime) / 3600 < max_age_hours:
        return json.loads(path.read_text())
    return None


def ffc(scoring_format: str = "ppr", max_age_hours: float = 6.0) -> dict:
    """FantasyFootballCalculator ADP -> {(name_key, pos): adp}."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"ffc_{SEASON}_{scoring_format}.json"
    data = _cached(path, max_age_hours)
    if data is None:
        r = requests.get(FFC.format(fmt=scoring_format, yr=SEASON), timeout=30)
        r.raise_for_status()
        data = r.json()
        path.write_text(json.dumps(data))
    return {key(p["name"], p["position"]): p["adp"] for p in data.get("players", [])}


def espn(projections: list[dict]) -> dict:
    """ESPN's own ADP, carried on the projection rows -> {(name_key, pos): adp}."""
    out = {}
    for p in projections:
        adp = p.get("adp")
        if adp and adp > 0:
            out[key(p["name"], p["position"])] = adp
    return out


def format_for(league) -> str:
    """Pick the FFC scoring format that best matches a league."""
    ppr = league.scoring.get("receptions", 0.0)
    if ppr >= 0.75:
        return "ppr"
    if ppr >= 0.25:
        return "half-ppr"
    return "standard"


def market_for(league, projections: list[dict]) -> tuple[dict, str]:
    """The right ADP source for this league. Returns (adp_map, source_label)."""
    if league.platform == "espn":
        m = espn(projections)
        if m:
            return m, "ESPN ADP"
    fmt = format_for(league)
    return ffc(fmt), f"FFC {fmt}"
