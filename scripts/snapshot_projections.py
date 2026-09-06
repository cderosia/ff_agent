#!/usr/bin/env python3
"""Freeze this week's projections before kickoff. Run it weekly.

    python3 scripts/snapshot_projections.py            # current week
    python3 scripts/snapshot_projections.py --week 4
    python3 scripts/snapshot_projections.py --accuracy # how are we doing?

The platforms overwrite weekly projections in place, so a week not captured
before its games is a week that can never be analysed. First write wins:
re-running is safe and will not overwrite an earlier, cleaner capture.

Best run Saturday. Sunday morning is fine. Sunday night is already too late for
the early games.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ff import projlog                        # noqa: E402


def current_week() -> int:
    import requests
    try:
        s = requests.get("https://api.sleeper.app/v1/state/nfl", timeout=15).json()
        return max(1, int(s.get("display_week") or s.get("week") or 1))
    except Exception:
        return 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--season", type=int, default=projlog.SEASON)
    ap.add_argument("--force", action="store_true",
                    help="re-take a snapshot that already exists (rarely right)")
    ap.add_argument("--accuracy", action="store_true",
                    help="score every stored week against what actually happened")
    args = ap.parse_args()

    if args.accuracy:
        res = projlog.accuracy(args.season)
        if "error" in res:
            print(f"  {res['error']}")
            return
        print(f"\nweeks compared: {res['weeks']}\n")
        print("FANTASY POINTS (reference PPR) — lower MAE is better")
        print(res["points"].to_string(index=False))
        print("\nPER STAT — which source predicts which field best")
        print(res["stats"].to_string(index=False))
        return

    week = args.week or current_week()
    p = projlog.snapshot(week, args.season, force=args.force)
    if p is None:
        existing = projlog.path_for(week, args.season)
        if existing.exists():
            print(f"  week {week} already captured: {existing.relative_to(ROOT)} "
                  f"(use --force to retake)")
        else:
            print(f"  week {week}: no source returned anything; nothing written")
        return

    import pandas as pd
    df = pd.read_parquet(p)
    n_by = df.groupby("source").size().to_dict()
    print(f"  wrote {p.relative_to(ROOT)}  —  {len(df)} rows, {n_by}")


if __name__ == "__main__":
    main()
