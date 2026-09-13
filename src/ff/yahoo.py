"""Yahoo fantasy reads, without the OAuth API.

Yahoo's OAuth host is behind a manual approval gate that has never opened for
this account -- every call returns 403 "not authorized". But Yahoo's own
frontend doesn't use that host. It reads from a public read-only mirror
authenticated by nothing but the ordinary session cookie:

    https://pub-api-ro.fantasysports.yahoo.com/fantasy/v2

Same /fantasy/v2 grammar, same ?format=json. Verified against this account:
unauthenticated calls return 401 "Unauthorized" -- NOT the 403 the gated OAuth
host returns -- which is the proof the entitlement gate isn't in play here.

Cookies: the pair that actually authenticates is T + Y. Measured by probing
combinations: A1+A3 returns 401 no matter the headers, T+Y alone returns 200.
A1/A3 are read too and sent when present, purely as insurance if Yahoo shifts
which cookie carries the session.

This is undocumented and Yahoo can change it without notice. Keep the OAuth
path in the tree; when credentials finally arrive this should be a swap behind
`league_settings()`, not a rewrite. `yahoo_auth.py` still holds that path.
"""
from __future__ import annotations

import functools

import requests

BASE = "https://pub-api-ro.fantasysports.yahoo.com/fantasy/v2"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0 Safari/537.36"}


class YahooAuthError(RuntimeError):
    """Raised loudly on purpose -- see get()."""


def cookies() -> dict:
    from .leagues import _env
    jar = {k: _env("YAHOO_" + k) for k in ("T", "Y", "A1", "A3")}
    return {k: v for k, v in jar.items() if v}


def get(path: str, timeout: int = 30) -> dict:
    """GET a fantasy/v2 path as JSON.

    Fails loudly on auth. This matters more than it looks: an unauthenticated
    league-list request returns 200 with ZERO leagues, which parses perfectly
    cleanly as "this user has no leagues". A completely broken session would
    otherwise be indistinguishable from a quiet week, and the weekly report
    would mail you an empty page instead of an error.
    """
    r = requests.get(f"{BASE}{path}?format=json", cookies=cookies(),
                     headers=UA, timeout=timeout)
    if r.status_code in (401, 403):
        raise YahooAuthError(
            f"Yahoo returned {r.status_code} for {path}. The session cookies in "
            ".env have expired -- refresh YAHOO_T and YAHOO_Y from DevTools > "
            "Application > Cookies on a Yahoo fantasy page.")
    r.raise_for_status()
    return r.json()


def _node(league_part, key):
    """Yahoo nests a league as [meta, {settings|teams|...}]. Pull one part out."""
    for part in league_part:
        if isinstance(part, dict) and key in part:
            v = part[key]
            return v[0] if isinstance(v, list) and v and isinstance(v[0], dict) else v
    return {}


@functools.lru_cache(maxsize=8)
def league_settings(league_key: str) -> dict:
    """Everything about a league, read live rather than typed in by hand.

    Returns a plain dict: teams, starters, scoring, guillotine, weeks. The
    scoring map is translated into this codebase's canonical stat vocabulary so
    it can be scored the same way as ESPN's and Sleeper's.
    """
    d = get(f"/league/{league_key}/settings")
    lg = d["fantasy_content"]["league"]
    meta = lg[0]
    s = _node(lg, "settings")

    cats = {str(c["stat"]["stat_id"]): c["stat"]
            for c in s.get("stat_categories", {}).get("stats", [])}
    scoring = {}
    for m in s.get("stat_modifiers", {}).get("stats", []):
        st = m["stat"]
        sid = str(st["stat_id"])
        canon = YAHOO_STAT.get(sid)
        if not canon:
            continue
        try:
            scoring[canon] = float(st.get("value"))
        except (TypeError, ValueError):
            continue

    starters, bench = {}, 0
    for rp in s.get("roster_positions", []):
        pos = rp["roster_position"]["position"]
        cnt = int(rp["roster_position"].get("count") or 0)
        if pos == "BN":
            bench = cnt
        elif pos == "IR":
            continue                    # IR doesn't affect replacement level
        else:
            starters[SLOT_ALIAS.get(pos, pos)] = cnt

    return {
        "name": meta.get("name"),
        "league_key": meta.get("league_key"),
        "teams": int(meta.get("num_teams") or 0),
        "starters": starters,
        "bench": bench,
        "scoring": scoring,
        "guillotine": str(meta.get("is_guillotine", s.get("is_guillotine", 0))) == "1",
        "start_week": int(meta.get("start_week") or 1),
        "end_week": int(meta.get("end_week") or 17),
        "current_week": int(meta.get("current_week") or 1),
        "uses_faab": str(s.get("uses_faab", 0)) == "1",
        "draft_status": meta.get("draft_status"),
        "waiver_day": s.get("waiver_rule"),
    }


# Yahoo stat_id -> this codebase's canonical name.
#
# The DST half used to be dropped here on the grounds that bucketed
# points-allowed tiers "don't survive translation". They survive fine as named
# tiers, and dropping them was expensive: a defense was priced off Sleeper's
# generic half-PPR number instead of this league's rules, and -- worse -- live
# scoring had no way to tell that Yahoo credits BOTH top tiers (Pts Allow 0 and
# Yds Allow 0-99) before a snap is played. That is 20 points of phantom baseline
# which decays to roughly zero in an average game, and the live projection was
# treating it as banked and adding a full pregame projection on top of it.
YAHOO_STAT = {
    "4": "pass_yds", "5": "pass_td", "6": "pass_int",
    "9": "rush_yds", "10": "rush_td",
    "11": "receptions", "12": "rec_yds", "13": "rec_td",
    "15": "ret_td", "16": "two_pt", "18": "fum_lost", "57": "fum_ret_td",
    # --- defense / special teams: events, which accrue as the game goes ---
    "32": "def_sack", "33": "def_int", "34": "def_fum_rec",
    "35": "def_td", "36": "def_safety", "37": "def_blk_kick",
    "49": "def_ret_td",
    # --- defense: STATE tiers, re-evaluated continuously, never banked ---
    "50": "pa_0", "51": "pa_1_6", "52": "pa_7_13", "53": "pa_14_20",
    "54": "pa_21_27", "55": "pa_28_34", "56": "pa_35",
    "71": "ya_0_99", "72": "ya_100_199", "73": "ya_200_299",
    "74": "ya_300_399", "75": "ya_400_499", "76": "ya_500",
}

# Yahoo's slot names -> ours.
SLOT_ALIAS = {"W/R/T": "FLEX", "W/R": "FLEX", "Q/W/R/T": "SUPERFLEX", "DEF": "DST"}
