"""What a waiver claim is worth in a guillotine league.

Normal FAAB advice is "hoard early, spend late". Guillotine inverts it, and the
reason is not sentiment: **your money is worthless the moment you are
eliminated**. So the horizon that matters is not weeks left in the season, it is
weeks you expect to still be alive -- and for a middling team that is a much
shorter number. For the work league at week 1 the two differ by 2.3x:

    budget / 15 weeks left      = $67 a week
    budget / 6.6 weeks alive    = $151 a week

Anyone dividing by the calendar is under-bidding by more than half.

THE UNIT. Points are the wrong currency here. A guillotine season ends when you
finish last once, so the honest question about any player is "how many more
weeks does he keep me alive", and that is directly simulable: run the season
with him and without him, and difference the expected weeks survived. A player
who adds nothing to your starting lineup adds zero weeks and is worth zero
regardless of his name.

    fair value = budget_left x (extra weeks he buys / weeks you'd survive without him)

That falls out of treating the budget as the price of your remaining life in the
league: if he extends your run by a fifth, he is worth a fifth of the budget.

ASSUMPTIONS, STATED. The elimination pool refreshes weekly and other managers
bid too; none of that is modelled, so `fair` is YOUR valuation, not a market
clearing price. Competition is handled crudely, by reporting what fraction of
the league also needs the position. The underlying season model is
winprob.season_odds, which does not injure anyone -- so it understates deep
teams (see its docstring for the measured bias).
"""
from __future__ import annotations

from . import draft as draft_mod
from . import winprob
from .names import key as nkey


def _team_totals(teams, by_key, league, byes, weekly_score):
    """(season points, weekly sd) per team, plus the index of yours."""
    totals, mine = [], 0
    for i, t in enumerate(teams):
        rs = [by_key[nkey(p["name"], p.get("position"))] for p in t.players
              if nkey(p["name"], p.get("position")) in by_key]
        filled = draft_mod._assign(rs, league)
        st = [q for sl, v in filled.items() if sl not in ("K", "DST") for q in v]
        var = sum(winprob.player_sd(q["points"] / 17.0, q["position"]) ** 2
                  for q in st)
        totals.append((weekly_score(rs, league, byes), var ** 0.5))
        if t.mine:
            mine = i
    return totals, mine


def guillotine_value(teams, by_key, league, byes, weekly_score, candidate,
                     budget_left: int, weeks_left: int,
                     from_team_id: str | None = None, sims: int = 4000) -> dict:
    """What `candidate` is worth to you, in dollars, this week.

    `from_team_id` is the eliminated roster he is coming off, so the simulation
    can remove him from it -- otherwise he is counted on two teams at once and
    the league looks stronger than it is.
    """
    from .rosters import Team

    base_totals, mi = _team_totals(teams, by_key, league, byes, weekly_score)
    before = winprob.season_odds(base_totals, mi, 0, weeks=weeks_left,
                                 guillotine=True, sims=sims)

    after_teams = []
    for t in teams:
        if t.mine:
            after_teams.append(Team(t.team_id, t.name, True, t.players + [
                {"name": candidate["name"], "position": candidate["position"],
                 "team": candidate.get("team")}]))
        elif from_team_id is not None and t.team_id == from_team_id:
            after_teams.append(Team(t.team_id, t.name, False, [
                p for p in t.players if p["name"] != candidate["name"]]))
        else:
            after_teams.append(t)
    a_totals, _ = _team_totals(after_teams, by_key, league, byes, weekly_score)
    after = winprob.season_odds(a_totals, mi, 0, weeks=weeks_left,
                                guillotine=True, sims=sims)

    w0 = max(before["weeks_survived"], 1e-6)
    extra = after["weeks_survived"] - before["weeks_survived"]
    share = max(0.0, extra / w0)
    fair = int(round(budget_left * share))

    # How many rivals plausibly want the same position: crude, but it is the
    # only competition signal available before bids are in.
    pos = candidate["position"]
    wanting = 0
    for t in teams:
        if t.mine:
            continue
        rs = [by_key[nkey(p["name"], p.get("position"))] for p in t.players
              if nkey(p["name"], p.get("position")) in by_key]
        base = draft_mod.lineup_value(rs, league, league.replacement)
        if draft_mod.lineup_value(rs + [candidate], league,
                                  league.replacement) - base > 0:
            wanting += 1
    rivals = wanting / max(1, len(teams) - 1)

    return {
        "player": candidate["name"], "position": pos,
        "weeks_before": round(before["weeks_survived"], 2),
        "weeks_after": round(after["weeks_survived"], 2),
        "extra_weeks": round(extra, 2),
        "fair": fair,
        # A contested player costs more than he is worth to any one bidder; this
        # is the most you should go, not a target.
        "max": int(round(fair * (1.0 + 0.5 * rivals))),
        "rivals_who_want_him": wanting,
        "rival_share": round(rivals, 2),
        "budget_per_live_week": int(round(budget_left / w0)),
        "budget_per_calendar_week": int(round(budget_left / max(1, weeks_left))),
    }
