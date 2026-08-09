"""Load each league's real settings from its platform into one shared shape.

The whole point of the project is that generic rankings are wrong for *your*
leagues. That only works if the settings are exactly right, so they are pulled
from the platform rather than typed by hand.
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field

import requests
import yaml

from .stats import (
    ESPN_SLOT,
    NON_STARTER_SLOTS,
    espn_scoring_to_canonical,
    sleeper_scoring_to_canonical,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
SEASON = 2026
ESPN_BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
UA = {"User-Agent": "Mozilla/5.0"}


@dataclass
class League:
    name: str
    platform: str
    league_id: str
    teams: int
    scoring: dict                      # canonical stat -> points
    starters: dict                     # slot name -> count
    bench: int = 0
    waiver_style: str = "priority"   # "priority" | "priority_reset" | "faab"
    waiver_note: str = ""
    draft_datetime: str = ""
    notes: str = ""
    raw: dict = field(default_factory=dict, repr=False)
    # filled in by vbd.build()
    replacement: dict = field(default_factory=dict, repr=False)
    starters_used: dict = field(default_factory=dict, repr=False)

    @property
    def starter_slots(self) -> int:
        return sum(self.starters.values())

    def describe(self) -> str:
        ppr = self.scoring.get("receptions", 0.0)
        fmt = {0.0: "standard", 0.5: "half-PPR", 1.0: "PPR"}.get(ppr, f"{ppr}/rec")
        slots = " ".join(f"{k}x{v}" for k, v in self.starters.items())
        return (f"{self.name:16} {self.platform:8} {self.teams:>2}tm  {fmt:9} "
                f"passTD={self.scoring.get('pass_td',0):g} "
                f"rushTD={self.scoring.get('rush_td',0):g}  [{slots}]")


def _env(key: str) -> str:
    """Read a value from .env without needing python-dotenv."""
    path = ROOT / ".env"
    if not path.exists():
        return os.environ.get(key, "")
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == key:
            return v.strip()
    return os.environ.get(key, "")


def load_sleeper(cfg: dict) -> League:
    lid = cfg["league_id"]
    d = requests.get(f"https://api.sleeper.app/v1/league/{lid}", timeout=30).json()
    if not d:
        raise RuntimeError(f"sleeper league {lid} not found")

    starters, bench = {}, 0
    for slot in d.get("roster_positions", []):
        if slot in ("BN", "IR", "TAXI"):
            bench += 1
            continue
        # Sleeper uses SUPER_FLEX / REC_FLEX naming
        name = {"SUPER_FLEX": "SUPERFLEX", "REC_FLEX": "WR/TE",
                "DEF": "DST", "WRRB_FLEX": "RB/WR"}.get(slot, slot)
        starters[name] = starters.get(name, 0) + 1

    s = d.get("settings") or {}
    # 0 = rolling priority, 1 = reverse-standings reset, 2 = FAAB.
    # NOTE: waiver_budget is populated (100) even when FAAB is off -- it's a
    # default, not evidence. Only waiver_type decides.
    style = {0: "priority", 1: "priority_reset", 2: "faab"}.get(s.get("waiver_type"), "priority")
    note = {"priority": "rolling — a successful claim drops you to last",
            "priority_reset": "resets weekly by record",
            "faab": "FAAB budget"}[style]

    return League(
        name=cfg["name"], platform="sleeper", league_id=str(lid),
        teams=d.get("total_rosters", 0), waiver_style=style, waiver_note=note,
        scoring=sleeper_scoring_to_canonical(d.get("scoring_settings")),
        starters=starters, bench=bench,
        draft_datetime=cfg.get("draft_datetime", ""), notes=cfg.get("notes", ""),
        raw=d,
    )


def load_espn(cfg: dict) -> League:
    lid = cfg["league_id"]
    cookies = {"espn_s2": _env("ESPN_S2"), "SWID": _env("ESPN_SWID")}
    url = f"{ESPN_BASE}/seasons/{SEASON}/segments/0/leagues/{lid}?view=mSettings"
    r = requests.get(url, headers=UA, cookies=cookies, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"espn league {lid}: HTTP {r.status_code} — check ESPN_S2/SWID in .env")
    s = r.json()["settings"]

    starters, bench = {}, 0
    for slot_id, count in (s["rosterSettings"]["lineupSlotCounts"] or {}).items():
        if not count:
            continue
        name = ESPN_SLOT.get(int(slot_id), f"slot{slot_id}")
        if name in NON_STARTER_SLOTS:
            bench += count
        else:
            starters[name] = starters.get(name, 0) + count

    acq = s.get("acquisitionSettings") or {}
    if acq.get("acquisitionType") == "WAIVERS_FAAB":
        style, note = "faab", "FAAB budget"
    elif acq.get("waiverOrderReset"):
        style, note = "priority_reset", "resets weekly by record"
    else:
        style, note = "priority", "rolling — a successful claim drops you to last"

    return League(
        name=cfg["name"], platform="espn", league_id=str(lid),
        teams=s.get("size", 0), waiver_style=style, waiver_note=note,
        scoring=espn_scoring_to_canonical(s["scoringSettings"].get("scoringItems")),
        starters=starters, bench=bench,
        draft_datetime=cfg.get("draft_datetime", ""), notes=cfg.get("notes", ""),
        raw=s,
    )


LOADERS = {"sleeper": load_sleeper, "espn": load_espn}


def load_all(path: pathlib.Path | None = None):
    """Load every configured league. Returns (leagues, errors)."""
    path = path or ROOT / "leagues" / "leagues.yaml"
    cfgs = yaml.safe_load(path.read_text())["leagues"]
    leagues, errors = [], []
    for cfg in cfgs:
        if not cfg.get("league_id") or not cfg.get("platform"):
            continue
        loader = LOADERS.get(cfg["platform"])
        if loader is None:
            errors.append((cfg["name"], f"no loader for platform '{cfg['platform']}'"))
            continue
        try:
            leagues.append(loader(cfg))
        except Exception as e:                      # keep going; report at the end
            errors.append((cfg["name"], str(e)))
    return leagues, errors
