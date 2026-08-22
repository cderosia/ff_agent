"""Expected starts: what a bench player is actually worth to you.

`lineup_value` counts only starters, so from about round 7 every remaining
player scores exactly 0 and the draft board stops discriminating. That is
correct as far as it goes -- a bench player contributes no starting-lineup
points this week -- but it throws away the whole reason you draft one.

A bench player is worth P(he ends up in your lineup) x (what he's worth when
he gets there). Two things put him there, and we can measure both:

  byes   -- known exactly from the schedule
  injury -- a rate assumption, stated below rather than hidden

That makes the fifth WR who covers your bye weeks beat the fifth TE who can
never crack the lineup, which raw points and VORP both get wrong.
"""
from __future__ import annotations

import csv
import functools
import math
import pathlib

from . import vbd
from .names import normalize
from .stats import SLOT_ELIGIBILITY

SEASON = 2026
DATA = pathlib.Path(__file__).resolve().parents[2] / "data" / "raw"

# Fantasy regular season. Weeks 15+ are playoffs in most of these leagues and
# a draft-day bench pick shouldn't be valued on games you may never play.
FANTASY_WEEKS = range(1, 15)

# Per-week probability a given starter is unavailable, MEASURED over 2018-2025
# by scripts/injury_rates.py: players drafted as starters by preseason ADP,
# counted absent in any week they took no offensive snap (n = 8,622
# player-weeks). Re-run that script to refresh these.
#
# These began as assumptions -- RB 0.14 / WR 0.10 / TE 0.10 / QB 0.07 -- and
# every one was far too low. The ordering survived (RBs do miss the most) but
# the spread did not: real risk is much flatter across positions than the
# assumption implied, so depth at EVERY position is worth more than the board
# used to think, and RB depth is not the outlier it was priced as.
#
# Note this is unavailability, not injury alone: a benched starter or one who
# loses his job counts, which is correct for the question being asked -- does a
# slot open up -- but broader than a medical injury rate.
MISS_RATE = {"RB": 0.22, "WR": 0.18, "TE": 0.20, "QB": 0.17}

# A player already on the field is genuinely next-man-up; one who barely played
# is a projection betting on a role he does not have yet. Scales the injury
# portion of expected starts only -- byes are certain regardless.
SNAP_FLOOR, SNAP_CEIL = 0.55, 1.15
NO_SNAP_DEFAULT = 0.35      # rookies and anyone absent from last year's data

# The projection feeds and the nflverse schedule disagree on a handful of team
# codes. Unmapped, those players silently look like they never have a bye,
# which is the worst kind of wrong: it fails quietly and inflates their value.
TEAM_ALIASES = {"LAR": "LA", "JAC": "JAX", "WSH": "WAS", "ARZ": "ARI",
                "OAK": "LV", "SD": "LAC", "STL": "LA", "BLT": "BAL",
                "HST": "HOU", "CLV": "CLE"}


def bye_for(team: str | None, byes: dict) -> int | None:
    """Bye week for a team, tolerant of feed-specific abbreviations."""
    if not team:
        return None
    if team in byes:
        return byes[team]
    return byes.get(TEAM_ALIASES.get(team, team))


@functools.lru_cache(maxsize=4)
def bye_by_team(season: int = SEASON) -> dict:
    """{team: bye week} for the season, straight from the schedule."""
    path = DATA / "nflverse_games.csv"
    played = {}
    teams = set()
    with path.open() as fh:
        for row in csv.DictReader(fh):
            if row["season"] != str(season) or not row["week"].isdigit():
                continue
            wk = int(row["week"])
            for side in ("home_team", "away_team"):
                t = row[side]
                teams.add(t)
                played.setdefault(t, set()).add(wk)
    byes = {}
    for t in teams:
        missing = [w for w in FANTASY_WEEKS if w not in played.get(t, ())]
        if missing:
            byes[t] = missing[0]
    return byes


@functools.lru_cache(maxsize=4)
def snap_share(season: int = SEASON - 1, last_n: int = 6) -> dict:
    """{normalized name: mean offensive snap share} over the last `last_n` weeks.

    Late-season usage, not full-season average: a player who took over a job in
    November is exactly the bench pick worth having.
    """
    path = DATA / f"nflverse_snaps_{season}.csv"
    if not path.exists():
        return {}
    rows = []
    with path.open() as fh:
        for r in csv.DictReader(fh):
            if r["position"] not in ("QB", "RB", "WR", "TE"):
                continue
            if not r["week"].isdigit():
                continue
            try:
                rows.append((normalize(r["player"]), int(r["week"]),
                             float(r["offense_pct"] or 0)))
            except ValueError:
                continue
    if not rows:
        return {}
    last_week = max(w for _, w, _ in rows)
    cutoff = last_week - last_n
    agg = {}
    for name, wk, pct in rows:
        if wk > cutoff:
            agg.setdefault(name, []).append(pct)
    return {n: sum(v) / len(v) for n, v in agg.items()}


def _opportunity(player: dict, snaps: dict) -> float:
    """How ready this player is to inherit a job, from last year's usage."""
    pct = snaps.get(normalize(player["name"]))
    if pct is None:
        return NO_SNAP_DEFAULT
    pct = max(0.0, min(1.0, pct))
    return SNAP_FLOOR + (SNAP_CEIL - SNAP_FLOOR) * pct


def _slots_for(position: str, league) -> int:
    """How many lineup slots this position can occupy at once."""
    n = 0
    for slot, cnt in league.starters.items():
        if position in SLOT_ELIGIBILITY.get(slot, {slot}):
            n += cnt
    return n


def _p_at_least(k: int, n: int, p: float) -> float:
    """P(at least k of n independent events, each with probability p)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    total = 0.0
    for i in range(k, n + 1):
        total += math.comb(n, i) * (p ** i) * ((1 - p) ** (n - i))
    return total


def expected_starts(player: dict, roster: list[dict], league,
                    snaps: dict | None = None) -> float:
    """Weeks this player is expected to be in your starting lineup.

    Walks the fantasy season week by week. He starts when the players ahead of
    him at his position are on bye or hurt in numbers that open a slot -- which
    is why a player whose bye lines up with your starters' is worth less than
    one who covers them.
    """
    snaps = snaps if snaps is not None else snap_share()
    byes = bye_by_team()
    pos = player["position"]
    slots = _slots_for(pos, league)
    if not slots:
        return 0.0

    ahead = [p for p in roster
             if p["position"] == pos and p["points"] > player["points"]]
    my_bye = bye_for(player.get("team"), byes)
    miss = MISS_RATE.get(pos, 0.10)
    opp = _opportunity(player, snaps)

    total = 0.0
    for wk in FANTASY_WEEKS:
        if my_bye == wk:
            continue                       # he can't start on his own bye
        live = [p for p in ahead if bye_for(p.get("team"), byes) != wk]
        if len(live) < slots:
            total += 1.0                   # a slot is open on merit or by bye
            continue
        # Everyone ahead is playing: he needs enough of them to be hurt.
        need = len(live) - slots + 1
        total += _p_at_least(need, len(live), miss) * opp
    return round(total, 2)


def waiver_level(rows: list[dict], league) -> dict:
    """{position: season points of the best player left AFTER the draft}.

    The right baseline for a bench pick is what you could stream off waivers,
    not a replacement level. Dynamic replacement is the best player still
    available, so measuring against it makes every bench value <= 0 by
    construction; static preseason replacement is a market that no longer
    exists by round 12. The player sitting at the edge of rosterable depth is
    the one actually on the wire in week 3.
    """
    depth = vbd.rosterable_depth(league)
    out = {}
    for pos, cap in depth.items():
        pool = sorted((r["points"] for r in rows if r["position"] == pos),
                      reverse=True)
        if pool:
            out[pos] = pool[min(cap, len(pool) - 1)]
    return out


def bench_value(player: dict, roster: list[dict], league, waiver: dict,
                snaps: dict | None = None) -> float:
    """Expected points this player adds over the season, starts included.

    Expected starts x what he's worth in a week he starts, over the streamer
    you'd otherwise play. Stays meaningful deep into a draft, where marginal
    lineup value is flat zero for everybody.
    """
    per_week = player["points"] / 17.0
    wire_week = waiver.get(player["position"], 0.0) / 17.0
    starts = expected_starts(player, roster, league, snaps)
    # A player worse than the streamer is worth 0, not a negative: you would
    # just play the streamer. Without the floor, covering more weeks makes a
    # bad player score WORSE, so the board prefers the one who shares your
    # bye week -- exactly backwards.
    return round(starts * max(0.0, per_week - wire_week), 2)
