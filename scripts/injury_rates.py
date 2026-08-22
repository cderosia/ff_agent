#!/usr/bin/env python3
"""Measure how often a drafted starter is unavailable, from nflverse.

ff.starts prices bench depth off a per-position miss rate. Those numbers began
as assumptions; this measures them.

Two traps, both of which inflate the rate if you ignore them:

  Survivorship -- ranking a season's starters by their FINAL points and asking
  how many games they missed answers the wrong question, because a back who
  tore an ACL in week 3 never makes the list. Picking them on weeks 1-4 usage
  is no better: that population is full of backups who had a hot month and then
  lost the job back, and a benched player reads as a missed game. We take the
  players the market DRAFTED as starters, by preseason ADP, which is exactly
  the population the draft board is reasoning about.

  Availability -- a missing stat line is not a missed game. A receiver who
  dresses and is never targeted records nothing, so measuring from the box
  score counts him absent. We measure from snap counts: a player took the
  field or he didn't.

    python3 scripts/injury_rates.py
"""
from __future__ import annotations

import collections
import csv
import json
import pathlib
import sys

import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ff.names import normalize          # noqa: E402
from ff.starts import TEAM_ALIASES      # noqa: E402

DATA = ROOT / "data" / "raw"
SEASONS = range(2018, 2026)
EARLY = range(1, 5)          # who was a starter
LATER = range(5, 18)         # what happened to him

# League-wide starter demand in a 12-team league -- the population whose
# availability the draft board actually cares about.
STARTERS = {"QB": 12, "RB": 30, "WR": 36, "TE": 12}


def fantasy_points(row) -> float:
    def f(k):
        try:
            return float(row.get(k) or 0)
        except ValueError:
            return 0.0
    return (f("passing_yards") * 0.04 + f("passing_tds") * 4
            - f("passing_interceptions") * 2
            + f("rushing_yards") * 0.1 + f("rushing_tds") * 6
            + f("receiving_yards") * 0.1 + f("receiving_tds") * 6
            + f("receptions") * 0.5)


def team_weeks(season: int) -> dict:
    """{team: set(weeks they played)} -- so a bye never counts as a miss."""
    out = collections.defaultdict(set)
    with (DATA / "nflverse_games.csv").open() as fh:
        for g in csv.DictReader(fh):
            if g["season"] != str(season) or not g["week"].isdigit():
                continue
            if g.get("game_type") not in (None, "", "REG"):
                continue
            wk = int(g["week"])
            out[g["home_team"]].add(wk)
            out[g["away_team"]].add(wk)
    return out


FFC = ("https://fantasyfootballcalculator.com/api/v1/adp/half-ppr"
       "?teams=12&year={yr}&position=all")


def drafted_starters(season: int) -> dict:
    """{position: [(normalized name, team)]} -- the top N at each position by ADP.

    Team comes from the ADP feed rather than from the box scores, so a starter
    who was hurt in August and never took a snap still counts. Reading his team
    off the stats would drop him from the sample entirely -- the same
    survivorship hole this script exists to avoid.
    """
    path = DATA / f"ffc_{season}_half-ppr.json"
    if path.exists():
        data = json.loads(path.read_text())
    else:
        # requests, not urllib: this Python install has no SSL roots
        # (see ANALYSIS.md), and requests carries certifi's bundle.
        r = requests.get(FFC.format(yr=season), timeout=30)
        r.raise_for_status()
        data = r.json()
        path.write_text(json.dumps(data))
    by_pos = collections.defaultdict(list)
    for pl in sorted(data.get("players", []), key=lambda p: p["adp"]):
        if pl["position"] in STARTERS:
            tm = pl.get("team") or ""
            by_pos[pl["position"]].append(
                (normalize(pl["name"]), TEAM_ALIASES.get(tm, tm)))
    return {pos: v[:STARTERS[pos]] for pos, v in by_pos.items()}


def season_rates(season: int) -> dict:
    path = DATA / f"nflverse_week_{season}.csv"
    snap_path = DATA / f"nflverse_snaps_{season}.csv"
    if not path.exists() or not snap_path.exists():
        return {}

    team_of = {}
    with path.open() as fh:
        for r in csv.DictReader(fh):
            pos = r.get("position")
            if pos not in STARTERS or not r["week"].isdigit():
                continue
            k = (normalize(r.get("player_display_name") or r.get("player_name")), pos)
            team_of.setdefault(k, r.get("team"))

    # Availability comes from the snap counts: he was on the field or he wasn't.
    played = collections.defaultdict(set)
    with snap_path.open() as fh:
        for r in csv.DictReader(fh):
            if r.get("position") not in STARTERS or not r["week"].isdigit():
                continue
            if r.get("game_type") not in (None, "", "REG"):
                continue
            try:
                snaps = float(r.get("offense_snaps") or 0)
            except ValueError:
                snaps = 0.0
            if snaps > 0:
                played[(normalize(r["player"]), r["position"])].add(int(r["week"]))

    tw = team_weeks(season)
    drafted = drafted_starters(season)
    out = {}
    for pos, n in STARTERS.items():
        chosen = drafted.get(pos, [])
        misses, chances = 0, 0
        for nm, tm in chosen:
            k = (nm, pos)
            weeks = tw.get(tm) or tw.get(team_of.get(k), set())
            for wk in LATER:
                if wk not in weeks:
                    continue                  # bye or no game
                chances += 1
                if wk not in played[k]:
                    misses += 1
        if chances:
            out[pos] = (misses / chances, chances)
    return out


def main():
    per_season = {}
    for s in SEASONS:
        r = season_rates(s)
        if r:
            per_season[s] = r
    if not per_season:
        print("no weekly data in data/raw/ -- fetch nflverse_week_YYYY.csv first")
        return

    print(f"Starter unavailability, weeks {LATER.start}-{LATER.stop - 1}.\n"
          f"Population: top {STARTERS} by preseason ADP (12-team half-PPR).\n"
          f"Available = took an offensive snap that week.\n")
    header = "  season   " + "".join(f"{p:>8}" for p in STARTERS)
    print(header)
    for s, r in per_season.items():
        print(f"  {s}     " + "".join(
            f"{r.get(p, (0, 0))[0] * 100:>7.1f}%" for p in STARTERS))
    print("  " + "-" * (len(header) - 2))
    pooled = {}
    for p in STARTERS:
        m = sum(r[p][0] * r[p][1] for r in per_season.values() if p in r)
        c = sum(r[p][1] for r in per_season.values() if p in r)
        pooled[p] = m / c if c else 0.0
        print(f"  pooled {p:>2}  {pooled[p] * 100:>6.1f}%   (n={c:,} player-weeks)")
    print("\nMISS_RATE = {" + ", ".join(
        f'"{p}": {pooled[p]:.2f}' for p in ("RB", "WR", "TE", "QB")) + "}")


if __name__ == "__main__":
    main()
