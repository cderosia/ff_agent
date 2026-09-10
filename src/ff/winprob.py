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


# ---------------------------------------------------------------------------
# League-wide odds
# ---------------------------------------------------------------------------
def project_team(team, week_pts: dict, league, week: int | None = None
                 ) -> tuple[float, float, list]:
    """(mean, sd, starters) for one team's best legal lineup this week.

    Scored under YOUR league's rules, not the platform's. That's the whole
    point: the same roster is worth different numbers in a 6-point-passing-TD
    half-PPR league than in the platform's generic view, and the platform's
    own projection can't tell you about a league it isn't scoring.
    """
    from .lineup import optimize
    from .names import key as nkey

    from .special import dst_week
    dsts = dst_week(week) if week else {}

    # Prefer the lineup the manager ACTUALLY set. Optimising everyone's roster
    # silently credits every rival with a perfect lineup, which inflates their
    # projection and understates your odds -- and it made the pregame and live
    # paths disagree on identical inputs, since the live one always used real
    # starters. Falls back to the optimiser where a platform reports no lineup.
    actual = [q for q in (team.starters or []) if q.get("name")]
    if actual:
        starters = []
        for q in actual:
            pos = (q.get("position") or "").upper()
            pos = "DST" if pos in ("DEF", "D/ST") else pos
            if pos == "DST":
                pts = dsts.get((q.get("team") or "").upper(), 0.0)
            else:
                pts, *_ = week_pts.get(nkey(q["name"], pos), (0.0, 0, 0.0))
            starters.append({**q, "position": pos, "week_points": pts})
        mu, sd = team_distribution(starters)
        return mu, sd, starters

    pool = []
    for p in team.players:
        pos = (p.get("position") or "").upper()
        if pos in ("DEF", "DST", "D/ST"):
            # Defenses are absent from the weekly blend; take them from the
            # same source ff.special uses for the draft board.
            pts = dsts.get((p.get("team") or "").upper(), 0.0)
            pool.append({**p, "position": "DST", "week_points": pts})
            continue
        pts, *_ = week_pts.get(nkey(p["name"], p["position"]), (0.0, 0, 0.0))
        pool.append({**p, "week_points": pts})
    filled, _bench = optimize(pool, league)
    starters = [p for slot in filled.values() for p in slot]
    mu, sd = team_distribution(starters)
    return mu, sd, starters


def season_odds(team_totals: list[tuple[float, float]], me: int,
                playoff_teams: int, weeks: int = 14, guillotine: bool = False,
                sims: int = 4000, seed: int = 2026,
                records: list[dict] | None = None,
                weeks_left: int | None = None,
                eliminated: list[bool] | None = None) -> dict:
    """Playoff / survival odds over a whole season.

    DIFFERENT QUESTION TO `league_odds`, AND THE NUMBERS WILL NOT MATCH.
    `league_odds` asks "am I safe THIS week" off this week's projections, which
    already carry byes and injury news. This asks "how does the season go" off
    season-long projections divided by the schedule, which is the right long-run
    mean but knows nothing about who is hurt today. Both are correct for their
    own question; presenting either as the other is what makes them look like a
    contradiction.

    `team_totals` is (season_points, weekly_sd) per team. Vectorised: weekly
    totals are drawn for every team and sim at once, so this is milliseconds
    rather than the minutes a per-player simulation takes.

    Head-to-head seeds on wins with points as the tiebreak, pairing randomly
    each week -- we do not model the real schedule, so this answers "a team this
    strong makes the playoffs X% of the time", not "you specifically will".

    KNOWN BIAS, MEASURED. This draws a team's weekly total directly, so it never
    injures anybody and never lets a bench player cover. That makes it blind to
    depth, and it therefore understates deep teams. Checked against the slow
    per-player simulation (gamma draws, MISS_RATE availability, lineup re-optimised
    every week) on all four head-to-head leagues:

        league            fast    slow    gap
        friends          78.2%   78.6%   -0.4
        family           83.9%   86.1%   -2.2
        719              67.5%   72.6%   -5.1
        freinds-keeper   53.1%   61.2%   -8.1

    The ordering survives and the picture is the same, but the error scales with
    bench quality -- freinds-keeper has the deepest bench of the five leagues,
    and is exactly where the gap is worst. Read these as a floor, not a estimate,
    for a team carrying real depth.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    n = len(team_totals)
    # Simulate only what is LEFT, and start from the record already banked.
    # Replaying the full season every week made an 8-1 team and a 1-8 team with
    # identical rosters read the same, which is precisely backwards.
    left = weeks if weeks_left is None else max(0, weeks_left)
    mu = np.array([t[0] / weeks for t in team_totals])
    sd = np.array([max(t[1], 1e-6) for t in team_totals])
    W = rng.normal(mu, sd, size=(sims, max(left, 1), n))
    if left == 0:
        W = W * 0.0
    banked_w = np.array([float((r or {}).get("wins", 0)) for r in
                         (records or [{}] * n)])
    banked_pf = np.array([float((r or {}).get("pf", 0)) for r in
                          (records or [{}] * n)])

    if guillotine:
        # A team already guillotined is out of the field for good; keeping it in
        # kept dividing your title odds by sixteen all season.
        start_alive = np.array([not bool(e) for e in
                                (eliminated or [False] * n)])
        alive = np.tile(start_alive, (sims, 1))
        survived = np.zeros((sims, n), dtype=int)
        for wi in range(left):
            wk = np.where(alive, W[:, wi, :], np.inf)
            loser = wk.argmin(axis=1)
            still = alive.sum(axis=1) > 1
            alive[np.arange(sims)[still], loser[still]] = False
            survived += alive.astype(int)
        return {"mode": "guillotine",
                "advance": float(alive[:, me].mean()),      # last team standing
                "weeks_survived": float(survived[:, me].mean()),
                "median_weeks": float(np.median(survived[:, me])),
                "out_first": float((survived[:, me] == 0).mean())}

    wins = np.tile(banked_w, (sims, 1)).astype(float)
    for wi in range(left):
        perm = np.argsort(rng.random((sims, n)), axis=1)
        a, b = perm[:, 0::2], perm[:, 1::2]
        k = min(a.shape[1], b.shape[1])
        a, b = a[:, :k], b[:, :k]
        wk = W[:, wi, :]
        sa = np.take_along_axis(wk, a, axis=1)
        sb = np.take_along_axis(wk, b, axis=1)
        np.add.at(wins, (np.arange(sims)[:, None], a), (sa > sb).astype(float))
        np.add.at(wins, (np.arange(sims)[:, None], b), (sb > sa).astype(float))
    season = W.sum(axis=1) + banked_pf
    order = np.lexsort((-season, -wins), axis=1)
    rank = np.empty_like(order)
    np.put_along_axis(rank, order, np.arange(1, n + 1)[None, :].repeat(sims, 0), axis=1)
    return {"mode": "head_to_head",
            "playoff": float((rank[:, me] <= playoff_teams).mean()),
            "first": float((rank[:, me] == 1).mean()),
            "last": float((rank[:, me] == n).mean()),
            "mean_wins": float(wins[:, me].mean()),
            "mean_rank": float(rank[:, me].mean())}



def live_project_team(team, week_pts: dict, league, live_row: dict | None,
                      progress: dict, week: int | None = None
                      ) -> tuple[float, float, list]:
    """(mean, sd, per-player detail) for a team MID-WEEK.

    Splits every starter into what is already settled and what is still to
    come. A finished player contributes his actual score and NO variance -- it
    is a fact, not a forecast. A player yet to kick off contributes his full
    projection and full variance. One mid-game contributes his points so far
    plus the remainder of his projection, carrying only the share of variance
    matching the football left to play.

        mean_i = scored_i + proj_i * f
        var_i  = (cv_i * proj_i)^2 * f          (f = fraction of game remaining)

    Variance falling linearly with time is the random-walk assumption. It has
    the two endpoints that matter: full spread before kickoff, none at the
    whistle. Between those it is an approximation, and `game_progress` documents
    why clock time only loosely tracks fantasy opportunity.

    Uses the lineup the manager ACTUALLY set where the platform reports it --
    points come from who is in the slots, not from who should be.
    """
    from .lineup import optimize
    from .livescore import remaining
    from .names import key as nkey
    from .special import dst_week

    dsts = dst_week(week) if week else {}
    scored = (live_row or {}).get("starters") or {}

    actual = [p for p in (team.starters or []) if p.get("name")]
    if actual:
        pool = []
        for p in actual:
            pos = (p.get("position") or "").upper()
            pos = "DST" if pos in ("DEF", "D/ST") else pos
            if pos == "DST":
                pts = dsts.get((p.get("team") or "").upper(), 0.0)
            else:
                pts, *_ = week_pts.get(nkey(p["name"], pos), (0.0, 0, 0.0))
            pool.append({**p, "position": pos, "week_points": pts})
        starters = pool
    else:
        _mu, _sd, starters = project_team(team, week_pts, league, week=week)

    mu = 0.0
    var = 0.0
    detail = []
    for p in starters:
        proj = p.get("week_points") or 0.0
        f = remaining(p.get("team"), progress)
        got = scored.get(nkey(p["name"], p.get("position")))
        got = 0.0 if got is None else float(got)
        m = got + proj * f
        sd = player_sd(proj, p.get("position")) * (f ** 0.5)
        mu += m
        var += sd ** 2
        detail.append({**p, "scored": got, "remaining": f,
                       "live_proj": round(m, 1)})
    return mu, var ** 0.5, detail


def league_odds(teams, week_pts: dict, league, opponent=None,
                week: int | None = None, live: dict | None = None,
                progress: dict | None = None) -> dict:
    """Your odds this week: head-to-head, or survival in a guillotine league.

    With `live` (per-team actual scores) and `progress` (fraction of each NFL
    game remaining) the odds become LIVE: banked points stop being uncertain and
    the percentage tightens as the day goes on. Without them the behaviour is
    exactly as before, so a caller that knows nothing about live scoring is
    unaffected.
    """
    dists, mine_i = [], None
    detail = []
    for i, t in enumerate(teams):
        if progress:
            mu, sd, starters = live_project_team(
                t, week_pts, league, (live or {}).get(t.team_id), progress,
                week=week)
        else:
            mu, sd, starters = project_team(t, week_pts, league, week=week)
        dists.append((mu, sd))
        detail.append({"team": t.name, "mine": t.mine, "proj": round(mu, 1),
                       "sd": round(sd, 1), "n_starters": len(starters),
                       "scored": round(sum(p.get("scored", 0)
                                           for p in starters), 1)
                                 if progress else None})
        if t.mine:
            mine_i = i
    if mine_i is None:
        return {"error": "couldn't identify your team"}

    out = {"teams": sorted(detail, key=lambda d: -d["proj"]),
           "mine": detail[mine_i]}
    if league.raw.get("guillotine"):
        out["mode"] = "guillotine"
        out.update(survive_elimination(dists, mine_i))
        # Rank 1 = highest projection, in every mode. Ranking ascending here
        # made last place read as first, which is the worst possible direction
        # to get backwards in an elimination league.
        ranked = sorted(range(len(dists)), key=lambda i: -dists[i][0])
        out["projected_rank"] = ranked.index(mine_i) + 1
        out["of"] = len(dists)
    elif opponent is not None:
        j = next((i for i, t in enumerate(teams) if t.team_id == opponent.team_id), None)
        out["mode"] = "head_to_head"
        out["opponent"] = opponent.name
        out["opponent_proj"] = round(dists[j][0], 1) if j is not None else None
        out["win"] = head_to_head(dists[mine_i], dists[j]) if j is not None else None
    else:
        # No matchup available -- still useful to know where you stand.
        out["mode"] = "field"
        ranked = sorted(range(len(dists)), key=lambda i: -dists[i][0])
        out["projected_rank"] = ranked.index(mine_i) + 1
        out["of"] = len(dists)
        out["beat_median"] = head_to_head(
            dists[mine_i], sorted(dists, key=lambda d: -d[0])[len(dists) // 2])
    return out
