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
    # Totals, carried so the missing-bucket residual below can be computed.
    # No league scores them, so they contribute nothing to a dot product.
    "fgm": "fg_made_total",
    "fga": "fg_att",
    "fgm_0_19": "fg_made_0_19",
    "fgm_20_29": "fg_made_20_29",
    "fgm_30_39": "fg_made_30_39",
    "fgm_40_49": "fg_made_40_49",
    "fgm_50p": "fg_made_50",
    # keepers and friends split the long end finer than anyone else, and both
    # keys were unmapped -- so a 55-yarder scored nothing in either league.
    "fgm_50_59": "fg_made_50_59",
    "fgm_60p": "fg_made_60",
    # --- defense / special teams ------------------------------------------
    # ff.special used to say Sleeper "exposes no K/DST scoring rules through
    # the API at all". That is not true and cost us a real ranking: every
    # Sleeper league publishes a full defensive scoring block, and the weekly
    # DEF projection carries a matching stat line (sack, int, ff, fum_rec,
    # pts_allow, yds_allow...). The keys were simply never mapped, so a defense
    # was priced off Sleeper's generic half-PPR total instead of your league's
    # rules -- where 719 pays 10 for a shutout and -4 for conceding 35.
    #
    # Canonical names are the ones YAHOO_STAT already established, so a Sleeper
    # projection can be scored under a Yahoo league's rules and vice versa.
    "sack": "def_sack",
    "int": "def_int",
    "ff": "def_ff",
    "fum_rec": "def_fum_rec",
    "safe": "def_safety",
    "blk_kick": "def_blk_kick",
    "def_td": "def_td",
    "st_td": "def_ret_td",
    "fum_rec_td": "fum_ret_td",
    # Deliberately NOT mapped: `def_st_td`, `def_st_ff`, `def_st_fum_rec`,
    # `def_kr_td`, `def_pr_td`. Each overlaps one of the above and Sleeper's
    # payload gives no way to tell whether they are additional or the same
    # event counted again -- a week-4 line carries def_kr_td 0.07, def_pr_td
    # 0.07 AND st_td 0.07, which is not a sum. Mapping two of them onto one
    # canonical name would silently double count, so only the unambiguous
    # total is taken.
    #
    # Points/yards-allowed TIERS are also absent on purpose. ff.special scores
    # them from the RAW `pts_allow` / `yds_allow` numbers through
    # `tier_value()`, which is the only approach that works across platforms:
    # Sleeper buckets yards 50 wide (`yds_allow_300_349`) and Yahoo 100 wide
    # (`ya_300_399`), so the indicator columns do not line up even though the
    # underlying yardage does.
    "pts_allow_0": "pa_0",
    "pts_allow_1_6": "pa_1_6",
    "pts_allow_7_13": "pa_7_13",
    "pts_allow_14_20": "pa_14_20",
    "pts_allow_21_27": "pa_21_27",
    "pts_allow_28_34": "pa_28_34",
    "pts_allow_35p": "pa_35",
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


# Made-FG buckets, short to long, in the canonical vocabulary.
FG_BUCKETS = ("fg_made_0_19", "fg_made_20_29", "fg_made_30_39",
              "fg_made_40_49", "fg_made_50", "fg_made_50_59", "fg_made_60")

# Where a recovered 50+ kick is credited, most preferred first. A league that
# splits 50-59 from 60+ gets the residual in the SHORTER band: it pays less
# (5 rather than 6 in both leagues that do this), and a 60-yard field goal is
# rare enough that assuming one would be the less honest guess.
_LONG_TARGETS = ("fg_made_50", "fg_made_50_59", "fg_made_60")


def fold_fg_buckets(line: dict, scoring: dict) -> dict:
    """Reconcile a kicker's made-FG buckets with the ones his league scores.

    Two things are wrong with a raw Sleeper kicker line, and together they cost
    `family` more than half of Chris Boswell: a week-2 projection of 3.3 where
    ESPN's app said 7.8.

    1. MISSING LONG BUCKET. Sleeper's weekly payload publishes 20-29, 30-39 and
       40-49 and no 50+ bucket at all -- across week 2, 31% of every made field
       goal projected league-wide fell into no bucket it publishes. In a league
       that scores by bucket those kicks were worth nothing. `fgm` carries the
       real total, so the residual is recovered here.

       ASSUMPTION, not a measurement: the residual is credited to 50+. It is
       the only bucket Sleeper never publishes, and the one other candidate is
       ruled out by the data -- `fgm_0_19` appears on 1 of 157 rows, because a
       sub-20-yard field goal barely exists. What is NOT established is the
       split's magnitude: ESPN's own season lines imply 55% of made kicks are
       50+ where this residual implies 31%, and ESPN's figure works out at
       roughly three times the league's real rate of made 50-yarders, so its
       bucket stat ids are themselves suspect. Crediting the residual is
       strictly better than scoring a third of all field goals at zero, and is
       stated here as the judgement call it is.

    2. BUCKET VOCABULARY. A league does not have to use the same bands as the
       feed. `family` scores one lumped 0-39 where Sleeper splits it three
       ways, so all three scored zero. Folding them in is exact -- the lump is
       precisely their sum -- and it is the larger half of the Boswell gap.

    Returns a new line; the input is not mutated.
    """
    out = dict(line)
    total = out.get("fg_made_total")
    if total:
        listed = sum(out.get(b, 0.0) for b in FG_BUCKETS)
        residual = float(total) - listed
        if residual > 0.01:
            tgt = next((b for b in _LONG_TARGETS if b in scoring), "fg_made_50")
            out[tgt] = out.get(tgt, 0.0) + residual

    # Fold each bucket the league does not score into the widest one it does
    # that still contains it. Only ever folds SHORTER bands upward into a lump,
    # never a long kick into a short band.
    if "fg_made_0_39" in scoring:
        short = sum(out.pop(b, 0.0) for b in
                    ("fg_made_0_19", "fg_made_20_29", "fg_made_30_39"))
        if short:
            out["fg_made_0_39"] = out.get("fg_made_0_39", 0.0) + short
    return out


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
