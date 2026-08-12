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


def ffc(scoring_format: str = "ppr", max_age_hours: float = 6.0) -> tuple[dict, dict]:
    """FantasyFootballCalculator ADP -> ({key: adp}, {key: adp_stdev})."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"ffc_{SEASON}_{scoring_format}.json"
    data = _cached(path, max_age_hours)
    if data is None:
        r = requests.get(FFC.format(fmt=scoring_format, yr=SEASON), timeout=30)
        r.raise_for_status()
        data = r.json()
        path.write_text(json.dumps(data))
    players = data.get("players", [])
    return ({key(p["name"], p["position"]): p["adp"] for p in players},
            {key(p["name"], p["position"]): p["stdev"] for p in players
             if p.get("stdev")})


# How far a player actually slides from his ADP. Fit on FFC's own published
# per-player stdev for 2026 (n=257, r=0.78): stdev ~= 0.102*adp + 1.13. Used for
# ESPN/Sleeper, which publish an average but no spread; FFC's real per-player
# number is preferred wherever we have it.
SD_SLOPE, SD_INTERCEPT = 0.102, 1.13


def spread_for(adp: float, known: float | None = None) -> float:
    """Standard deviation of a player's real draft position."""
    if known:
        return float(known)
    return max(1.0, SD_SLOPE * float(adp or 0) + SD_INTERCEPT)


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


def sleeper(projections: list[dict], scoring_format: str = "ppr") -> dict:
    """Sleeper's own ADP, carried on its projection payload.

    Sleeper has no standalone ADP endpoint, but the projections feed includes
    adp_ppr / adp_half_ppr / adp_std -- the real price in Sleeper drafts, which
    beats a generic blended proxy for a Sleeper league.
    """
    out = {}
    for p in projections:
        adps = p.get("sleeper_adp") or {}
        v = adps.get(scoring_format)
        # Sleeper uses 999/1000 as its undrafted sentinel
        if v and 0 < v < 900:
            out[key(p["name"], p["position"])] = v
    return out


def market_for(league, projections: list[dict]) -> tuple[dict, str, dict]:
    """The right ADP source for this league. Returns (adp_map, label, stdev_map).

    Prefer the platform's own market -- that's the room you're actually drafting
    in. FFC is only a fallback when the platform publishes nothing usable.
    Only FFC publishes a per-player stdev; the others fall back to `spread_for`.
    """
    fmt = format_for(league)
    if league.platform == "espn":
        m = espn(projections)
        if m:
            return m, "ESPN ADP", {}
    if league.platform == "sleeper":
        m = sleeper(projections, fmt)
        if len(m) >= 100:                  # enough depth to rank against
            return m, f"Sleeper ADP ({fmt})", {}
    m, sd = ffc(fmt)
    return m, f"FFC {fmt}", sd
