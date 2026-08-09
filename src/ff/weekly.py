"""In-season weekly analysis: usage trends, free agents, waiver targets.

The premise: box scores are lagging indicators. By the time a running back has a
big game, the wire has already noticed. Opportunity -- snap share, target share,
carries -- moves first. So we rank waiver targets on usage trend, not points.
"""
from __future__ import annotations

import pathlib

import pandas as pd
import requests

from .names import key
from .stats import score

ROOT = pathlib.Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "raw"
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"

# nflverse weekly column -> canonical stat
NFLVERSE_STAT = {
    "passing_yards": "pass_yds", "passing_tds": "pass_td",
    "passing_interceptions": "pass_int", "passing_2pt_conversions": "pass_2pt",
    "rushing_yards": "rush_yds", "rushing_tds": "rush_td",
    "rushing_2pt_conversions": "rush_2pt",
    "receptions": "receptions", "receiving_yards": "rec_yds",
    "receiving_tds": "rec_td", "receiving_2pt_conversions": "rec_2pt",
    "fumbles_lost_total": "fum_lost",
}


def _download(url: str, dest: pathlib.Path) -> pathlib.Path:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        dest.write_bytes(r.content)
    return dest


def weekly_stats(season: int) -> pd.DataFrame:
    p = _download(f"{NFLVERSE}/stats_player/stats_player_week_{season}.csv",
                  CACHE / f"nflverse_week_{season}.csv")
    return pd.read_csv(p, low_memory=False)


def snap_counts(season: int) -> pd.DataFrame:
    p = _download(f"{NFLVERSE}/snap_counts/snap_counts_{season}.csv",
                  CACHE / f"nflverse_snaps_{season}.csv")
    return pd.read_csv(p, low_memory=False)


def usage_trend(season: int, through_week: int, recent: int = 3) -> pd.DataFrame:
    """Per player: recent vs. prior usage. The leading indicator for waivers.

    Only uses weeks <= through_week, so replaying a past week can't peek ahead.
    """
    wk = weekly_stats(season)
    wk = wk[(wk.week <= through_week) & wk.position.isin(["QB", "RB", "WR", "TE"])]
    sn = snap_counts(season)
    sn = sn[sn.week <= through_week][["player", "week", "team", "offense_pct"]]

    wk = wk.merge(sn.rename(columns={"player": "player_display_name"}),
                  on=["player_display_name", "week", "team"], how="left")

    cut = through_week - recent
    wk["phase"] = wk.week.gt(cut).map({True: "recent", False: "prior"})

    agg = (wk.groupby(["player_display_name", "position", "phase"])
             .agg(snap_pct=("offense_pct", "mean"),
                  targets=("targets", "mean"),
                  tgt_share=("target_share", "mean"),
                  carries=("carries", "mean"),
                  games=("week", "nunique"))
             .reset_index())
    piv = agg.pivot_table(index=["player_display_name", "position"],
                          columns="phase",
                          values=["snap_pct", "targets", "tgt_share", "carries", "games"])
    piv.columns = [f"{a}_{b}" for a, b in piv.columns]
    piv = piv.reset_index().fillna(0.0)
    piv["snap_delta"] = piv.get("snap_pct_recent", 0) - piv.get("snap_pct_prior", 0)
    piv["tgt_delta"] = piv.get("targets_recent", 0) - piv.get("targets_prior", 0)
    piv["carry_delta"] = piv.get("carries_recent", 0) - piv.get("carries_prior", 0)
    return piv


def recent_points(season: int, through_week: int, scoring: dict,
                  recent: int = 3) -> dict:
    """{(name,pos): avg fantasy points over the last `recent` weeks}."""
    wk = weekly_stats(season)
    wk = wk[(wk.week <= through_week) & (wk.week > through_week - recent)
            & wk.position.isin(["QB", "RB", "WR", "TE"])]
    out = {}
    for _, r in wk.iterrows():
        line = {c: float(r[k] or 0) for k, c in NFLVERSE_STAT.items()
                if k in wk.columns and pd.notna(r[k])}
        k2 = key(r.player_display_name, r.position)
        out.setdefault(k2, []).append(score(line, scoring))
    return {k: sum(v) / len(v) for k, v in out.items()}


def rostered(league_id: str) -> set:
    """Every player_id on a roster in this league."""
    rs = requests.get(f"https://api.sleeper.app/v1/league/{league_id}/rosters",
                      timeout=30).json()
    out = set()
    for r in rs:
        out |= set(r.get("players") or [])
    return out


def trending_adds(hours: int = 24, limit: int = 50) -> dict:
    """{player_id: add count} across all of Sleeper -- market heat, live only."""
    r = requests.get(
        f"https://api.sleeper.app/v1/players/nfl/trending/add"
        f"?lookback_hours={hours}&limit={limit}", timeout=30)
    return {x["player_id"]: x["count"] for x in r.json()} if r.ok else {}


def faab_bid(rank: int, budget_left: int, weeks_left: int, heat: float) -> int:
    """A suggested opening bid, as a percent of remaining budget.

    Deliberately simple and legible: top targets get real money, depth gets
    scraps, and heavy market interest pushes the bid up. Better to be
    explainable than falsely precise.
    """
    base = {0: 0.28, 1: 0.18, 2: 0.12, 3: 0.08}.get(rank, 0.04)
    base *= 1.0 + min(heat, 1.0) * 0.5
    # late in the season, hoard less
    if weeks_left <= 5:
        base *= 1.35
    return max(1, int(round(budget_left * base)))
