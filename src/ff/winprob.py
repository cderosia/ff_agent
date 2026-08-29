"""Odds of winning this week, and of surviving an elimination league.

A projection is a mean, and a mean alone can't answer the question you actually
have on Sunday morning: am I likely to win? Two lineups projected 118 and 112
are not a 6-point favourite in any useful sense -- fantasy weeks are wild, and
that margin is inside a single receiver's good afternoon.

So we carry a distribution, not a point estimate: each starter contributes a
mean and a spread, the team total is their sum, and the matchup is the
difference of two such totals.

The spreads are MEASURED, not assumed -- src/ff/winprob.py's constants come
from nflverse weekly scoring, 2019-2025, over players with 8+ games and a 6+
ppg average (the startable population this is actually about):

    pos    players   median CV (sd / mean)
    QB         242        0.44
    RB         353        0.60
    WR         505        0.65
    TE         157        0.67

Quarterbacks are the steadiest scorers and pass-catchers the wildest, which is
the well-known shape and a decent sign the measurement isn't broken.

TWO HONEST LIMITS.

Independence: we add variances as though players never move together. Real
lineups are correlated -- a quarterback and his own receiver boom in the same
game -- so a stacked lineup's true spread is wider than this says, and a
diversified one's is close to right. That makes reported probabilities slightly
too confident, most of all for stacked teams.

Projections are the sources', so the mean is only as good as they are. This
quantifies the uncertainty AROUND a projection; it cannot rescue a bad one.
"""
from __future__ import annotations

import math
import random

# sd = CV x projected points. K and DST are estimates, not measurements: no
# clean weekly series was to hand, and they matter little either way.
CV = {"QB": 0.44, "RB": 0.60, "WR": 0.65, "TE": 0.67, "K": 0.45, "DST": 0.75}
CV_DEFAULT = 0.62

# A projection of zero still has upside, and a tiny one is nearly all noise, so
# a floor keeps a benched-then-started player from looking like a certainty.
SD_FLOOR = 1.5


def player_sd(points: float, position: str | None) -> float:
    return max(SD_FLOOR, abs(points) * CV.get((position or "").upper(), CV_DEFAULT))


def team_distribution(starters: list[dict], key: str = "week_points"
                      ) -> tuple[float, float]:
    """(mean, sd) for a starting lineup's weekly total."""
    mu = sum(p.get(key) or 0 for p in starters)
    var = sum(player_sd(p.get(key) or 0, p.get("position")) ** 2 for p in starters)
    return mu, math.sqrt(var)


def _phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def head_to_head(mine: tuple[float, float], theirs: tuple[float, float]) -> float:
    """P(you outscore them). Difference of two normals, so exact -- no sampling."""
    (mu_a, sd_a), (mu_b, sd_b) = mine, theirs
    sd = math.sqrt(sd_a ** 2 + sd_b ** 2)
    if sd <= 0:
        return 1.0 if mu_a > mu_b else 0.0
    return _phi((mu_a - mu_b) / sd)


def survive_elimination(teams: list[tuple[float, float]], me: int,
                        sims: int = 40000, seed: int = 0) -> dict:
    """P(you are NOT the week's lowest scorer), for a guillotine league.

    Simulated rather than solved: "lowest of N" has no tidy closed form once
    the teams have different spreads, and 40k draws settles it to well inside
    the error the projections themselves carry.
    """
    rnd = random.Random(seed)
    survived = 0
    worst_margin = []
    for _ in range(sims):
        draws = [rnd.gauss(mu, sd) for mu, sd in teams]
        mine = draws[me]
        low = min(draws)
        if mine > low:
            survived += 1
        others = [d for i, d in enumerate(draws) if i != me]
        worst_margin.append(mine - min(others))
    worst_margin.sort()
    return {
        "advance": survived / sims,
        "eliminated": 1 - survived / sims,
        # how far clear of last you finish, at the median and in a bad week
        "median_cushion": round(worst_margin[len(worst_margin) // 2], 1),
        "cushion_10th_pct": round(worst_margin[len(worst_margin) // 10], 1),
    }
