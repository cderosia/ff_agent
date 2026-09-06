"""Canonical stat vocabulary, and translation from each platform's dialect.

Every platform names the same underlying stats differently. Rather than special-
case scoring per platform downstream, we translate both the *projections* and the
*scoring rules* into one shared vocabulary here, then scoring is a plain dot
product anywhere else in the codebase.
"""

# ---------------------------------------------------------------------------
# ESPN numeric statId -> canonical name.
# Only the ones that carry fantasy points in common formats.
# ---------------------------------------------------------------------------
ESPN_STAT = {
    0: "pass_att",
    1: "pass_cmp",
    3: "pass_yds",
    4: "pass_td",
    19: "pass_2pt",
    20: "pass_int",
    23: "rush_att",
    24: "rush_yds",
    25: "rush_td",
    26: "rush_2pt",
    41: "targets",
    42: "rec_yds",
    43: "rec_td",
    44: "rec_2pt",
    53: "receptions",
    58: "rec_target",
    68: "fumbles",
    72: "fum_lost",
    # kicking
    74: "fg_made_0_39",
    77: "fg_made_40_49",
    80: "fg_made_50",
    85: "fg_missed",
    86: "xp_made",
    88: "xp_missed",
    # ESPN publishes made-FG-under-40 under a SECOND id as well, and some
    # leagues score only that one: the family league awards 5 points on 198 and
    # nothing on 74, so leaving it unmapped silently dropped every short field
    # goal and understated its kickers by about a point and a half a week.
    # Verified equal, not assumed -- in the raw payload 198/199/200 carry byte
    # identical values to 74/75/76 (Brandon Aubrey, week 1: both 0.307496245)
    # and appear on kickers only. Mapping both to one canonical name is safe
    # because a stat line is a dict: the second write overwrites with the same
    # number rather than double counting it.
    198: "fg_made_0_39",
}

# ---------------------------------------------------------------------------
# Sleeper scoring_settings key -> canonical name.
# Sleeper uses the same keys for scoring rules and stat lines.
# ---------------------------------------------------------------------------
SLEEPER_STAT = {
    "pass_yd": "pass_yds",
    "pass_td": "pass_td",
    "pass_int": "pass_int",
    "pass_2pt": "pass_2pt",
    "pass_att": "pass_att",
    "pass_cmp": "pass_cmp",
    "rush_yd": "rush_yds",
    "rush_td": "rush_td",
    "rush_2pt": "rush_2pt",
    "rush_att": "rush_att",
    "rec": "receptions",
    "rec_yd": "rec_yds",
    "rec_td": "rec_td",
    "rec_2pt": "rec_2pt",
    "fum_lost": "fum_lost",
    "fum": "fumbles",
    # Kickers. Sleeper scores field goals by DISTANCE (fgm_yds, points per
    # yard) where ESPN's projection only reports made-FG buckets, so these
    # can't be scored directly -- ff.special approximates from bucket
    # midpoints. Carried through anyway so the league's real rules are
    # visible rather than absent.
    "fgm_yds": "fg_yds",
    "xpm": "xp_made",
    "xpmiss": "xp_missed",
    "fgmiss": "fg_missed",
    "fgm_0_19": "fg_made_0_19",
    "fgm_20_29": "fg_made_20_29",
    "fgm_30_39": "fg_made_30_39",
    "fgm_40_49": "fg_made_40_49",
    "fgm_50p": "fg_made_50",
}

# ESPN lineup slot id -> slot name
ESPN_SLOT = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE",
    7: "SUPERFLEX", 16: "DST", 17: "K", 20: "BENCH", 21: "IR", 23: "FLEX",
}

# Which real positions may legally fill a slot. Drives replacement level.
SLOT_ELIGIBILITY = {
    "QB": {"QB"},
    "RB": {"RB"},
    "WR": {"WR"},
    "TE": {"TE"},
    "K": {"K"},
    "DST": {"DST"},
    "FLEX": {"RB", "WR", "TE"},
    "RB/WR": {"RB", "WR"},
    "WR/TE": {"WR", "TE"},
    "SUPERFLEX": {"QB", "RB", "WR", "TE"},
    "TQB": {"QB"},
}

NON_STARTER_SLOTS = {"BENCH", "IR"}


def score(stat_line: dict, scoring: dict) -> float:
    """Fantasy points for a canonical stat line under canonical scoring rules."""
    return sum(v * scoring.get(k, 0.0) for k, v in stat_line.items())


def espn_scoring_to_canonical(scoring_items) -> dict:
    """ESPN `scoringItems` -> {canonical_stat: points_per_unit}."""
    out = {}
    for item in scoring_items or []:
        name = ESPN_STAT.get(item.get("statId"))
        if name is None:
            continue
        pts = item.get("pointsOverrides", {}).get("16", item.get("points", 0.0))
        if pts:
            out[name] = float(pts)
    return out


def sleeper_scoring_to_canonical(scoring_settings: dict) -> dict:
    """Sleeper `scoring_settings` -> {canonical_stat: points_per_unit}."""
    out = {}
    for key, pts in (scoring_settings or {}).items():
        name = SLEEPER_STAT.get(key)
        if name and pts:
            out[name] = float(pts)
    return out
