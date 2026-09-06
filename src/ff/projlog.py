"""Archive of what every source predicted, so the blend can be audited later.

WHY THIS EXISTS. The platforms overwrite their weekly projections in place.
Once a week kicks off, what ESPN and Sleeper *said* on Saturday is gone --
there is no endpoint that returns it. So the only way to ever answer "is our
blend actually better than either source alone" is to write it down before the
games, every week, and never overwrite it. A season of that is ~24k rows; a
season of NOT doing it is a question that can never be answered.

WHAT IS STORED. One Parquet file per (season, week), holding one row per
(source, player) with the CANONICAL stat line -- not points. Points are a pure
linear function of (stats x scoring), so storing stats keeps every league's
scoring derivable after the fact, and survives a league changing its rules
mid-season. Reference point columns are included for eyeballing only.

Sources: `espn`, `sleeper`, and `blend` -- ours. The blend row is stored
explicitly rather than recomputed, so a later change to how we blend can't
quietly rewrite history.

BLEND EQUIVALENCE. lineup.weekly_points scores each source's line and averages
the POINTS. Scoring is linear, so that equals scoring the averaged STAT LINE --
provided a stat missing from one source counts as zero in that source's line,
which is what weekly_points does implicitly. `_blend_line` reproduces exactly
that (divide by the number of source lines, not by how many carried the stat),
so the stored blend row scores identically to what the app showed.

KNOWN GAP. Defenses have no stat line anywhere; they are stored points-only,
with `points_ref` set and stat columns null. Kickers carry a real line.
"""
from __future__ import annotations

import datetime as _dt
import pathlib
from typing import Iterable

import pandas as pd

from .names import key as nkey
from .stats import score

ROOT = pathlib.Path(__file__).resolve().parents[2]
# NOT under data/ -- that whole tree is gitignored as a refetchable cache, and
# this is the opposite of refetchable. Losing it loses the season.
LOG_DIR = ROOT / "projlog"
SEASON = 2026

STAT_COLS = [
    "pass_att", "pass_cmp", "pass_yds", "pass_td", "pass_int", "pass_2pt",
    "rush_att", "rush_yds", "rush_td", "rush_2pt",
    "receptions", "rec_target", "targets", "rec_yds", "rec_td", "rec_2pt",
    "fumbles", "fum_lost",
    "fg_made_0_19", "fg_made_20_29", "fg_made_30_39", "fg_made_0_39",
    "fg_made_40_49", "fg_made_50", "fg_missed", "fg_yds",
    "xp_made", "xp_missed",
]

# Reference scorings only -- for a quick look, never for a league decision.
# Anything league-specific is recomputed from the stat line via `points`.
REF_PPR = {"pass_yds": 0.04, "pass_td": 4, "pass_int": -2, "pass_2pt": 2,
           "rush_yds": 0.1, "rush_td": 6, "rush_2pt": 2, "receptions": 1.0,
           "rec_yds": 0.1, "rec_td": 6, "rec_2pt": 2, "fum_lost": -2}

# nflverse weekly -> our canonical vocabulary. Only the fields a projection
# actually predicts; nflverse's EPA/air-yards columns have no counterpart.
ACTUAL_MAP = {
    "attempts": "pass_att", "completions": "pass_cmp",
    "passing_yards": "pass_yds", "passing_tds": "pass_td",
    "passing_interceptions": "pass_int",
    "passing_2pt_conversions": "pass_2pt",
    "carries": "rush_att", "rushing_yards": "rush_yds",
    "rushing_tds": "rush_td", "rushing_2pt_conversions": "rush_2pt",
    "receptions": "receptions", "targets": "rec_target",
    "receiving_yards": "rec_yds", "receiving_tds": "rec_td",
    "receiving_2pt_conversions": "rec_2pt",
}


def path_for(week: int, season: int = SEASON) -> pathlib.Path:
    return LOG_DIR / f"{season}" / f"week{week:02d}.parquet"


def _blend_line(lines: list[dict]) -> dict:
    """Mean stat line over sources, missing counted as zero. See module docs."""
    out: dict = {}
    n = len(lines)
    for stat in {k for ln in lines for k in ln}:
        out[stat] = sum(ln.get(stat, 0.0) for ln in lines) / n
    return out


def points(row_or_df, scoring: dict):
    """Fantasy points for a stored row or whole frame under any scoring."""
    if isinstance(row_or_df, pd.DataFrame):
        total = pd.Series(0.0, index=row_or_df.index)
        for stat, coef in scoring.items():
            if stat in row_or_df.columns:
                # Columns arrive as object dtype when a snapshot had gaps (a
                # kicker carries no pass_yds); coerce before arithmetic rather
                # than letting fillna silently downcast.
                col = pd.to_numeric(row_or_df[stat], errors="coerce").fillna(0.0)
                total = total + col * coef
        return total
    return score({k: v for k, v in row_or_df.items()
                  if k in STAT_COLS and pd.notna(v)}, scoring)


def snapshot(week: int, season: int = SEASON, force: bool = False,
             dst: bool = True) -> pathlib.Path | None:
    """Freeze this week's projections from every source. First write wins.

    Idempotent on purpose: re-running mid-week must not overwrite the Saturday
    capture with a Sunday one whose numbers have already started moving toward
    the result. Pass `force` only to deliberately re-take a snapshot.
    """
    out_path = path_for(week, season)
    if out_path.exists() and not force:
        return None

    from .lineup import espn_weekly, sleeper_weekly
    per_source: dict[str, dict] = {}
    for name, fn in (("espn", espn_weekly), ("sleeper", sleeper_weekly)):
        try:
            per_source[name] = fn(week)
        except Exception as e:                 # one dead feed must not lose the rest
            print(f"  ! projlog: {name} week {week} unavailable ({type(e).__name__})")
    if not per_source:
        return None

    rows = []
    captured = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    all_keys = {k for d in per_source.values() for k in d}
    for k in all_keys:
        name, pos = k
        lines = [d[k] for d in per_source.values() if k in d]
        for src, d in per_source.items():
            if k in d:
                rows.append({"source": src, "name": name, "position": pos, **d[k]})
        rows.append({"source": "blend", "name": name, "position": pos,
                     **_blend_line(lines)})

    if dst:
        try:
            from .special import dst_week
            for team, pts in (dst_week(week) or {}).items():
                for src in ("sleeper", "blend"):
                    rows.append({"source": src, "name": team, "position": "DST",
                                 "team": team, "points_ref": float(pts)})
        except Exception as e:
            print(f"  ! projlog: DST week {week} unavailable ({type(e).__name__})")

    df = pd.DataFrame(rows)
    for c in STAT_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    df["season"], df["week"], df["captured_at"] = season, week, captured
    ref = points(df, REF_PPR)
    # DST rows already carry their own points; don't overwrite with a zero.
    df["points_ref"] = df.get("points_ref", pd.Series(pd.NA, index=df.index)).fillna(ref)

    cols = (["season", "week", "captured_at", "source", "name", "position",
             "points_ref"] + STAT_COLS)
    df = df[[c for c in cols if c in df.columns]]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    return out_path


def load(season: int = SEASON, weeks: Iterable[int] | None = None) -> pd.DataFrame:
    """Every snapshot on disk for a season."""
    base = LOG_DIR / str(season)
    if not base.exists():
        return pd.DataFrame()
    files = sorted(base.glob("week*.parquet"))
    frames = [pd.read_parquet(f) for f in files]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if weeks is not None:
        df = df[df.week.isin(list(weeks))]
    return df


def actuals(season: int, weeks: Iterable[int] | None = None) -> pd.DataFrame:
    """What actually happened, in the same vocabulary as the projections."""
    from .weekly import weekly_stats         # same cached nflverse pull
    d = weekly_stats(season)
    d = d[d.week.isin(list(weeks))] if weeks is not None else d
    out = pd.DataFrame({
        "week": d["week"],
        "name": d["player_display_name"],
        "position": d["position"],
    })
    for src_col, canon in ACTUAL_MAP.items():
        out[canon] = d[src_col].fillna(0.0) if src_col in d.columns else 0.0
    return out


def accuracy(season: int = SEASON, weeks: Iterable[int] | None = None,
             scoring: dict | None = None) -> dict:
    """Per-source error against what happened. The whole point of the archive.

    Returns two frames:
      `points` -- MAE and bias in fantasy points per source, under `scoring`
                  (default reference PPR). Answers "is the blend better".
      `stats`  -- MAE per source PER STAT. Answers the more useful question:
                  which source predicts which field best, which is what a
                  weighted blend would need in order to be worth building.

    Bias is signed (projection minus actual), so a source that is merely
    optimistic looks different from one that is noisy -- they want different
    fixes.
    """
    proj = load(season, weeks)
    if proj.empty:
        return {"error": "no snapshots on disk yet"}
    try:
        act = actuals(season, sorted(proj.week.unique()))
    except Exception:
        # nflverse publishes a season's file only once games exist, so before
        # week 1 this is a 404 rather than an empty frame. Not an error worth
        # a traceback -- it is simply too early, and will answer itself.
        act = pd.DataFrame()
    if act.empty:
        return {"error": f"no completed {season} games to compare against yet — "
                         f"{len(proj)} projection rows are banked and waiting"}

    proj = proj.copy()
    proj["k"] = [nkey(n, p) for n, p in zip(proj.name, proj.position)]
    act = act.copy()
    act["k"] = [nkey(n, p) for n, p in zip(act.name, act.position)]

    sc = scoring or REF_PPR
    proj["proj_pts"] = points(proj, sc)
    act["act_pts"] = points(act, sc)

    m = proj.merge(act[["week", "k", "act_pts"]
                       + [c for c in ACTUAL_MAP.values() if c in act.columns]],
                   on=["week", "k"], suffixes=("", "_act"), how="inner")

    pts_rows = []
    for src, g in m.groupby("source"):
        err = g["proj_pts"] - g["act_pts"]
        pts_rows.append({"source": src, "n": len(g),
                         "MAE": round(err.abs().mean(), 2),
                         "bias": round(err.mean(), 2),
                         "RMSE": round((err ** 2).mean() ** 0.5, 2)})
    stat_rows = []
    for canon in ACTUAL_MAP.values():
        acol = f"{canon}_act"
        if canon not in m.columns or acol not in m.columns:
            continue
        for src, g in m.groupby("source"):
            e = g[canon].astype(float).fillna(0.0) - g[acol].astype(float).fillna(0.0)
            stat_rows.append({"stat": canon, "source": src,
                              "MAE": round(e.abs().mean(), 3),
                              "bias": round(e.mean(), 3)})
    return {
        "points": pd.DataFrame(pts_rows).sort_values("MAE").reset_index(drop=True),
        "stats": pd.DataFrame(stat_rows).sort_values(["stat", "MAE"]
                                                     ).reset_index(drop=True),
        "weeks": sorted(m.week.unique().tolist()),
    }
