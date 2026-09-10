#!/usr/bin/env python3
"""FF Agent — one app across every league.

    streamlit run app.py

Sidebar picks the league; every tab re-derives from that league's own scoring
and roster settings. Nothing here uses FAAB or money: all five leagues run
priority waivers, so the currency is your waiver position.
"""
from __future__ import annotations

import json
import pathlib
import time
import sys

import pandas as pd
import streamlit as st
import yaml

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from ff import board as board_mod
from ff import rosters as rosters_mod          # noqa: E402
from ff import draft as draft_mod          # noqa: E402
from ff import keepers as keeper_mod       # noqa: E402
from ff import trades as trade_mod         # noqa: E402
from ff import weekly as weekly_mod        # noqa: E402
from ff import projlog as projlog_mod      # noqa: E402
from ff import winprob as winprob_mod      # noqa: E402
from ff.leagues import _env, load_all      # noqa: E402
from ff.names import key                   # noqa: E402
from ff.projections import fetch           # noqa: E402
from ff import themes as th                # noqa: E402
from ff import livescore as ls_mod         # noqa: E402

st.set_page_config(page_title="FF Agent", page_icon="🏈", layout="wide")

st.markdown(th.global_css(), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# cached loaders
# ---------------------------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner="loading projections…")
def get_projections():
    return fetch()


@st.cache_data(ttl=900, show_spinner="loading league settings…")
def get_leagues():
    leagues, errors = load_all()
    return [l.__dict__ for l in leagues], errors


@st.cache_data(ttl=21600, show_spinner="loading player index…")
def get_blob():
    """Sleeper's player index -- and, critically, its INJURY feed.

    This was `cache_resource` with no TTL over a file that was never re-fetched,
    so the injury data went a month stale without a symptom: on the morning of
    week 1 it had Brock Bowers healthy (Sleeper live: Doubtful, knee), TreVeyon
    Henderson healthy (live: Out, ankle) and Jahmyr Gibbs questionable (live:
    fine). Wrong in both directions, which is the worst kind -- it hid two real
    injuries and invented a third. Now refreshed on a 6h TTL, on disk and in
    process.
    """
    import requests
    p = ROOT / "data" / "raw" / "sleeper_players.json"
    fresh_enough = (p.exists() and
                    (time.time() - p.stat().st_mtime) < 21600)
    if fresh_enough:
        return json.loads(p.read_text())
    try:
        d = requests.get("https://api.sleeper.app/v1/players/nfl",
                         timeout=180).json()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d))
        return d
    except Exception:
        if p.exists():                      # stale beats nothing
            return json.loads(p.read_text())
        raise


@st.cache_data(ttl=900)
def get_configs():
    return {c["name"]: c for c in
            yaml.safe_load((ROOT / "leagues" / "leagues.yaml").read_text())["leagues"]}


def leagues_objects():
    """load_all() returns dataclasses; cache them as dicts and rehydrate."""
    from ff.leagues import League
    raw, errors = get_leagues()
    return [League(**d) for d in raw], errors


@st.cache_data(ttl=600, show_spinner=False)
def get_board(league_name: str):
    leagues, _ = leagues_objects()
    L = next(x for x in leagues if x.name == league_name)
    rows, meta = board_mod.build(get_projections(), L)
    return rows, meta, L.replacement, L.starters_used


def hydrate(league_name):
    """League object with replacement levels filled in (board.build sets them)."""
    leagues, _ = leagues_objects()
    L = next(x for x in leagues if x.name == league_name)
    rows, meta, repl, used = get_board(league_name)
    L.replacement, L.starters_used = repl, used
    return L, rows, meta


@st.cache_data(ttl=600, show_spinner=False)
def nfl_state() -> dict:
    """Authoritative week from Sleeper. Beats each platform's own answer:
    ESPN and Yahoo disagree during the preseason, and a league that hasn't
    started reports week 0 or 1 interchangeably."""
    import requests
    try:
        return requests.get("https://api.sleeper.app/v1/state/nfl",
                            timeout=15).json()
    except Exception:
        return {}


def _default_week() -> int:
    w = nfl_state().get("display_week") or nfl_state().get("week")
    if w:
        return max(1, int(w))
    ls, _ = leagues_objects()
    for l in ls:
        if l.raw.get("current_week"):
            return int(l.raw["current_week"])
    return 1


@st.cache_data(ttl=1800, show_spinner=False)
def trend_season(week: int) -> tuple[int, int, bool]:
    """(season, through_week, is_live) for usage trends.

    nflverse only publishes a season once games are played, so before week 1
    there is no 2026 file at all -- asking for it 404s. Fall back to the last
    completed season and SAY SO rather than showing last year's numbers as if
    they were this year's, which is what the old `replay` toggle did by
    defaulting to on. Flips itself to live data the week it exists."""
    from ff import weekly as _w
    if week > 1:
        try:
            _w.usage_trend(2026, week - 1)
            return 2026, week - 1, True
        except Exception:
            pass
    return 2025, 17, False


@st.cache_data(ttl=1800, show_spinner=False)
def archive_week(week: int) -> str:
    """Freeze this week's projections the first time the app runs in it.

    The platforms overwrite weekly projections in place, so a week that is not
    captured before kickoff can never be analysed afterwards -- there is no
    endpoint that returns what ESPN said last Saturday. Opening the app is the
    one thing that reliably happens every week, so the capture rides on it.
    First write wins, so this cannot overwrite a cleaner earlier snapshot, and
    when the file already exists it costs a single stat() call.
    """
    try:
        made = projlog_mod.snapshot(week)
        if made is not None:
            return f"captured week {week}"
        return ("already captured" if projlog_mod.path_for(week).exists()
                else "nothing to capture yet")
    except Exception as e:
        return f"failed: {type(e).__name__}"


# ---------------------------------------------------------------------------
# sidebar
# ---------------------------------------------------------------------------
leagues, errors = leagues_objects()
cfgs = get_configs()

st.sidebar.title("🏈 FF Agent")
if not leagues:
    st.sidebar.error("No leagues loaded.")
    st.stop()

# Home and Sunday are not properties of any one league -- they are the whole
# Sunday, across all five. Making them tabs INSIDE a league meant picking an
# arbitrary league to view them from, which was meaningless. They are now
# top-level destinations and the league picker only appears once you are in a
# league.
names = [l.name for l in leagues]
NAV = ["Home", "Sunday"]
nav = st.sidebar.radio("View", NAV + ["\u2014 league \u2014"], index=0, key="nav",
                       label_visibility="collapsed")
in_league = nav not in NAV
choice = st.sidebar.radio("League", names, index=0, key="league",
                          disabled=not in_league)
L, rows, meta = hydrate(choice)
cfg = cfgs.get(choice, {})

ppr = L.scoring.get("receptions", 0.0)
fmt = {0.0: "standard", 0.5: "half-PPR", 1.0: "PPR"}.get(ppr, f"{ppr}/rec")
st.sidebar.caption(
    f"**{L.teams}-team {fmt}** on {L.platform}  \n"
    f"pass TD {L.scoring.get('pass_td',0):g} · rush/rec TD {L.scoring.get('rush_td',0):g}  \n"
    f"waivers: {L.waiver_note}"
)
st.sidebar.divider()
if st.sidebar.button("Refresh data", width='stretch'):
    # st.cache_data.clear() alone was close to cosmetic: the projections live in
    # on-disk files with their own TTL, defenses sat behind a process-lifetime
    # cache, and the player index is cache_resource, so none of them moved. That
    # is why the site could show a stale number and pressing refresh changed
    # nothing. Force every layer.
    st.cache_data.clear()
    st.cache_resource.clear()
    try:   # force the player/injury index to re-download
        (ROOT / 'data' / 'raw' / 'sleeper_players.json').unlink(missing_ok=True)
    except Exception:
        pass
    try:
        from ff import special as _sp
        _sp.dst_week.cache_clear()
        _sp.dst_early.cache_clear()
    except Exception:
        pass
    try:
        from ff import lineup as _lp
        _wk = _default_week()
        _lp.espn_weekly(_wk, force=True)
        _lp.sleeper_weekly(_wk, force=True)
    except Exception as _e:
        st.sidebar.warning(f"refetch failed: {type(_e).__name__}")
    st.rerun()
for nm, err in errors:
    st.sidebar.warning(f"{nm}: {err[:60]}")

st.sidebar.divider()
_ns = nfl_state()
st.sidebar.caption(
    f"**{_ns.get('season','2026')} week {_ns.get('display_week','?')}** "
    f"({_ns.get('season_type','')}). Rosters are read live from each platform. "
    "Usage trends fall back to 2025 until this season has games; every tab "
    "that does says so."
)
def _feed_age() -> str:
    import time as _t
    out = []
    for label, fn in (("ESPN", f"espn_proj_{2026}_raw.json"),
                      ("Sleeper", f"sleeper_wk_{2026}_w{_default_week()}.json")):
        f = ROOT / "data" / "raw" / fn
        if f.exists():
            mins = (_t.time() - f.stat().st_mtime) / 60
            out.append(f"{label} {mins:.0f}m" if mins < 90
                       else f"{label} {mins/60:.1f}h")
        else:
            out.append(f"{label} —")
    return " · ".join(out)


st.sidebar.caption(f"**Projections last pulled:** {_feed_age()} ago. "
                   "Weekly numbers move on game day — hit Refresh if a player "
                   "looks wrong.")

_arch = archive_week(_default_week())
_n_weeks = len(list((projlog_mod.LOG_DIR / "2026").glob("week*.parquet"))) \
    if (projlog_mod.LOG_DIR / "2026").exists() else 0
st.sidebar.caption(
    f"**Projection archive** — {_arch}; {_n_weeks} week"
    f"{'s' if _n_weeks != 1 else ''} banked. Every source's numbers are frozen "
    "before kickoff so the blend can be scored against them at season's end."
)
st.sidebar.info(
    "**Draft day** runs separately:\n\n`python3 scripts/draft_server.py`\n\n"
    "It's a glanceable second screen that refreshes itself, with its own "
    "league switcher."
)


# ---------------------------------------------------------------------------
# tabs
# ---------------------------------------------------------------------------
# Board lives in scripts/draft_server.py, not here -- the drafts are done.
# Sunday and Watch were one question asked twice, so they are one tab now.
# ---- Report ---------------------------------------------------------------
# One page, no controls. The whole point is that Sunday morning you open this
# and it already says what to do -- picking a week and a report type and then
# pressing Build was three decisions before any answer appeared.
@st.cache_data(ttl=30, show_spinner=False)
def _games(week: int):
    from ff import live
    return live.games(week=week, season=2026)


@st.cache_data(ttl=120, show_spinner="reading your rosters…")
def _holdings(week: int):
    from ff import live
    from ff.leagues import load_all as _la
    ls, _ = _la()
    return live.my_holdings(ls, week, get_blob())


@st.cache_data(ttl=86400, show_spinner=False)
def byes_all():
    from ff import starts as _s
    return _s.bye_by_team()


@st.cache_data(ttl=3600, show_spinner=False)
def regular_weeks(league_name: str) -> int:
    """Weeks the season actually runs before playoffs.

    Matters most for the guillotine league: 16 teams need 15 eliminations to
    leave one standing, so simulating 14 weeks makes winning impossible and
    reports whatever share of teams are merely still alive at week 14 as though
    it were the title chance.
    """
    import requests
    L_, _r, _m = hydrate(league_name)
    try:
        if L_.raw.get("guillotine"):
            end = int(L_.raw.get("end_week") or 15)
            start = int(L_.raw.get("start_week") or 1)
            return max(L_.teams - 1, end - start + 1)
        if L_.platform == "sleeper":
            s_ = requests.get(f"https://api.sleeper.app/v1/league/{L_.league_id}",
                              timeout=20).json()["settings"]
            return int(s_.get("playoff_week_start") or 15) - 1
        if L_.platform == "espn":
            from ff.leagues import _env as _e
            ck = {"espn_s2": _e("ESPN_S2"), "SWID": _e("ESPN_SWID")}
            u = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons"
                 f"/2026/segments/0/leagues/{L_.league_id}")
            s_ = requests.get(u, params={"view": "mSettings"}, cookies=ck,
                              timeout=30).json()["settings"]["scheduleSettings"]
            return int(s_.get("matchupPeriodCount") or 14)
    except Exception:
        pass
    return 14


@st.cache_data(ttl=3600, show_spinner=False)
def playoff_count(league_name: str) -> int | None:
    """How many teams make the playoffs. None for the guillotine league."""
    import requests
    L_, _r, _m = hydrate(league_name)
    try:
        if L_.platform == "sleeper":
            s_ = requests.get(f"https://api.sleeper.app/v1/league/{L_.league_id}",
                              timeout=20).json()["settings"]
            return int(s_.get("playoff_teams") or 6)
        if L_.platform == "espn":
            from ff.leagues import _env as _e
            ck = {"espn_s2": _e("ESPN_S2"), "SWID": _e("ESPN_SWID")}
            u = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons"
                 f"/2026/segments/0/leagues/{L_.league_id}")
            s_ = requests.get(u, params={"view": "mSettings"}, cookies=ck,
                              timeout=30).json()["settings"]["scheduleSettings"]
            return int(s_.get("playoffTeamCount") or 6)
    except Exception:
        return None
    return None


@st.cache_data(ttl=900, show_spinner=False)
def faab_balance(league_name: str) -> int | None:
    """Live FAAB left. Only the Yahoo league has a budget at all."""
    L_, _r, _m = hydrate(league_name)
    if L_.waiver_style != "faab":
        return None
    try:
        from ff import yahoo as _y
        d = _y.get(f"/league/{L_.raw['league_key']}/teams")
        node = d["fantasy_content"]["league"][1]["teams"]
        for k in node:
            if k == "count":
                continue
            flat = {}
            for it in node[k]["team"][0]:
                if isinstance(it, dict):
                    flat.update(it)
            if flat.get("is_owned_by_current_login"):
                return int(flat.get("faab_balance") or 0)
    except Exception:
        pass
    return int(L_.raw.get("faab_budget") or 0)


@st.cache_data(ttl=30, show_spinner=False)
def live_scores(league_name: str, week: int):
    """Actual points, straight from the platform. 30s TTL -- fantasy scores
    move on a scoring play, not continuously."""
    L_, _r, _m = hydrate(league_name)
    return ls_mod.scores(L_, week, get_blob())


@st.cache_data(ttl=45, show_spinner=False)
def game_progress(week: int) -> dict:
    """{TEAM: fraction of his game still to play}. Drives live odds."""
    try:
        return ls_mod.game_progress(_games(week))
    except Exception:
        return {}


@st.cache_data(ttl=60, show_spinner=False)
def kickoff_states(week: int) -> dict:
    """{TEAM: pre|in|post}. Lets a zero mean "played and scored nothing"
    rather than "hasn't kicked off", which are opposite facts."""
    try:
        return ls_mod.team_states(_games(week))
    except Exception:
        return {}


@st.cache_data(ttl=60, show_spinner=False)
def games_started(week: int) -> bool:
    try:
        return ls_mod.any_started(_games(week))
    except Exception:
        return False


@st.cache_data(ttl=600, show_spinner="building your report…")
def build_report(league_name: str, week: int):
    """Lineup + waivers for one league in a single pass.

    Cross-platform on purpose: the emailed report goes through
    send_weekly.gather(), which talks to Sleeper directly and so has never
    worked for the ESPN or Yahoo leagues. This uses the same rosters module the
    Lineup and Waivers tabs already use, so all five leagues render.
    """
    from ff import lineup as lineup_mod
    L_, rows_, _meta = hydrate(league_name)
    blob = get_blob()
    by_key = {key(r["name"], r["position"]): r for r in rows_}

    teams = rosters_mod.all_teams(L_, week, blob)
    if not rosters_mod.has_drafted(L_, teams):
        return {"drafted": False}
    mine, _others = rosters_mod.board_rosters(L_, by_key, week, blob)
    if not mine:
        return {"drafted": False}

    # --- lineup ---
    # build_pool carries kickers and defenses; board rows do not (ff.special),
    # so anything built off board rows leaves two starting slots empty.
    wp = lineup_mod.weekly_points(week, L_.scoring)
    my_team = next((t for t in teams if t.mine), None)
    pool = lineup_mod.build_pool(L_, my_team.players if my_team else [],
                                 week, by_key, blob)
    filled, bench = lineup_mod.optimize(pool, L_)
    my_actual = (my_team.starters if my_team else [])
    diff = lineup_mod.actual_vs_optimal(pool, filled, my_actual)
    calls = lineup_mod.close_calls(filled, bench, L_)
    total = sum(p["week_points"] for ps in filled.values() for p in ps)

    # Odds. winprob carries a DISTRIBUTION rather than a point estimate, which
    # is the only way to answer "am I likely to win" -- two lineups at 118 and
    # 112 are not a 6-point favourite in any useful sense.
    try:
        opp = rosters_mod.opponent_of(L_, week, teams)
        # Once anything has kicked off the odds stop being a pregame number:
        # banked points lose their variance and the percentage tightens through
        # the day. With nothing started this is byte-identical to before.
        _prog = game_progress(week)
        _any_live = any(v < 1.0 for v in (_prog or {}).values())
        _livesc = ls_mod.scores(L_, week, blob) if _any_live else {}
        odds = winprob_mod.league_odds(
            teams, wp, L_, opponent=opp, week=week,
            live=_livesc if _any_live else None,
            progress=_prog if _any_live else None)
        odds["is_live"] = _any_live
        _my_id = my_team.team_id if my_team else None
        _opp_id = opp.team_id if opp else None
    except Exception as e:
        odds = {"error": f"{type(e).__name__}: {e}"}
        _my_id = my_team.team_id if my_team else None
        _opp_id = None

    # Season odds are a DIFFERENT question and will not match the weekly number:
    # this week's projection knows who is hurt and who is on bye, the season one
    # is the long-run mean. Both are shown, labelled, rather than reconciled.
    try:
        from bakeoff import weekly_score as _wsc
        totals, mi = [], 0
        for i_, t_ in enumerate(teams):
            rs = [by_key[key(p["name"], p.get("position"))] for p in t_.players
                  if key(p["name"], p.get("position")) in by_key]
            f_ = draft_mod._assign(rs, L_)
            stt = [q for sl, v in f_.items() if sl not in ("K", "DST") for q in v]
            # /17: weekly_score spreads season points over 17 games, not 14 weeks
            var = sum(winprob_mod.player_sd(q["points"] / 17.0, q["position"]) ** 2
                      for q in stt)
            totals.append((_wsc(rs, L_, byes_all()), var ** 0.5))
            if t_.mine:
                mi = i_
        season_out = winprob_mod.season_odds(
            totals, mi, playoff_count(league_name) or 6,
            weeks=regular_weeks(league_name),
            guillotine=bool(L_.raw.get("guillotine")))
    except Exception as e:
        # NB: named season_out, not season -- the waiver block below rebinds
        # `season` to an int year via trend_season(), which silently turned this
        # dict into 2025 and blew up the renderer.
        season_out = {"error": f"{type(e).__name__}: {e}"}

    # --- waivers ---
    season, through, live = trend_season(week)
    taken = {key(pl["name"], pl.get("position"))
             for t in teams for pl in t.players}
    cands, risers = [], []
    try:
        trend = weekly_mod.usage_trend(season, through)
        trend["k"] = [key(n, pp) for n, pp in
                      zip(trend.player_display_name, trend.position)]
        ppg = weekly_mod.recent_points(season, through, L_.scoring)
        trend["ppg"] = trend.k.map(ppg).fillna(0.0)
        heat = weekly_mod.heat_rank(blob) if live else {}
        base = draft_mod.lineup_value(mine, L_, L_.replacement)
        for r in trend.itertuples():
            if r.k in taken or r.snap_pct_recent <= 0.25:
                continue
            row = by_key.get(r.k)
            if row is None:
                continue
            gain = draft_mod.lineup_value(mine + [row], L_, L_.replacement) - base
            risers.append({"Player": row["name"], "Pos": row["pos_rank"],
                           "PPG": round(r.ppg, 1),
                           "Snap%": f"{r.snap_pct_recent*100:.0f}%",
                           "ΔSnap": f"{r.snap_delta*100:+.0f}",
                           "Tgt": round(r.targets_recent, 1),
                           "_d": r.snap_delta})
            if gain <= 0:
                continue
            call, why = weekly_mod.claim_call(gain, heat.get(r.k),
                                              L_.waiver_style, live)
            cands.append({"Player": row["name"], "Pos": row["pos_rank"],
                          "Adds": round(gain), "PPG": round(r.ppg, 1),
                          "Snap%": f"{r.snap_pct_recent*100:.0f}%",
                          "ΔSnap": f"{r.snap_delta*100:+.0f}",
                          "Call": call, "Why": why, "_g": gain})
        cands.sort(key=lambda c: -c["_g"])
        risers.sort(key=lambda c: -c["_d"])
    except Exception as e:
        cands = [{"error": f"{type(e).__name__}: {e}"}]

    drops = sorted(mine, key=lambda r: r["vorp"])[:5]
    return {"drafted": True, "total": total, "filled": filled, "bench": bench,
            "calls": calls, "cands": cands[:10], "risers": risers[:10],
            "drops": drops, "odds": odds,
            # "season" was ALREADY taken by the waiver block's trend year, and a
            # dict literal keeps the last duplicate key -- so the odds dict was
            # being silently replaced by the int 2025.
            "season_odds": season_out, "diff": diff,
            "_my_id": _my_id, "_opp_id": _opp_id,
            # NFL teams behind each roster's starters, so a viewer can tell
            # per side whether anyone has kicked off yet.
            "_starter_teams": {
                t_.team_id: [str(x.get("team") or "").upper()
                             for x in (t_.starters or [])]
                for t_ in teams},
            "playoff_cut": playoff_count(league_name),
            "season": season, "through": through, "live": live,
            "unavailable": [p for p in pool if p["status"] in ("out", "unknown")]}


URGENT_HELP = (
    "A starter who is out or projecting zero, an empty starting slot, a lineup "
    "change worth making, or elimination risk in the guillotine league. A close "
    "call is not listed: two players inside a projection's noise are both "
    "defensible, and flagging those trains you to ignore the flags."
)


def _flags(rep, name) -> list[dict]:
    """Everything in one league that wants a decision, worst first."""
    out = []
    if not rep.get("drafted"):
        return out
    L_, _r, _m = hydrate(name)
    d = rep.get("diff") or {}
    for b in d.get("broken", []):
        out.append({"sev": 0, "text": f"**{b.get('slot') or b['position']}: "
                    f"{b['name']}** is "
                    f"{(b.get('status') or 'unavailable').upper()}"
                    + (f" — {b['why']}" if b.get("why") else "")
                    + f" · that slot scores {b.get('week_points', 0):.1f}"})
    for sl in d.get("empty_slots", []):
        out.append({"sev": 0, "text": f"**{sl}** has nobody in it"})
    for sw in d.get("swaps", []):
        bad = sw["out_status"] in ("out", "unknown")
        # One line, not two: the reason he has to come out and the instruction
        # for who replaces him are the same decision.
        why = ""
        if bad:
            why = f" is {(sw['out_status'] or '').upper()}"
            if sw.get("out_why"):
                why += f" ({sw['out_why'].replace('ESPN: ', '')})"
        out.append({"sev": 0 if bad else 1,
                    "text": (f"**{sw['out']}**{why} ({sw['out_pts']}) → start "
                             f"**{sw['in']}** ({sw['in_pts']})"
                             f"  ·  **{sw['gain']:+.1f} pts**")
                            if bad else
                            (f"Bench **{sw['out']}** ({sw['out_pts']}) → start "
                             f"**{sw['in']}** ({sw['in_pts']})"
                             f"  ·  **{sw['gain']:+.1f} pts**")})
    o = rep.get("odds") or {}
    if o.get("mode") == "guillotine" and o.get("eliminated", 0) >= 0.12:
        out.append({"sev": 1, "text": f"Elimination risk "
                    f"**{o['eliminated']*100:.0f}%** this week"})
    return sorted(out, key=lambda x: x["sev"])


# ---- Sunday ---------------------------------------------------------------
# Was two tabs, "Sunday" and "Watch", asking one question: what is on and who
# of mine is in it. Merged.
# ---- Lineup ---------------------------------------------------------------


if nav == "Home":
    wk_all = _default_week()
    st.markdown(th.css(scope="home"), unsafe_allow_html=True)
    st.markdown(f"### Week {wk_all}")

    LIVE = games_started(wk_all)
    KST = kickoff_states(wk_all)
    cards, todo, total_flags = [], [], 0
    for lg in leagues:
        try:
            r = build_report(lg.name, wk_all)
        except Exception as e:
            cards.append((lg.name, "error", f"{type(e).__name__}: {e}",
                          None, None, None))
            continue
        if not r.get("drafted"):
            cards.append((lg.name, "none", "not drafted", None, None, None))
            continue
        o = r.get("odds") or {}
        sea = r.get("season_odds") or {}
        mode = o.get("mode")
        if mode == "guillotine":
            head, sub = f"{o['advance']*100:.0f}%", "safe this week"
            extra = (f"{sea['advance']*100:.0f}% win it"
                     if sea.get("advance") is not None else "")
            opp = "lowest scorer goes out"
        elif mode == "head_to_head" and o.get("win") is not None:
            head, sub = f"{o['win']*100:.0f}%", "to win"
            extra = (f"{sea['playoff']*100:.0f}% playoffs"
                     if sea.get("playoff") is not None else "")
            opp = f"vs {o['opponent']}"
        else:
            head, sub, extra, opp = "—", "", "", ""
        mine_p = o.get("mine", {}).get("proj")
        them_p = o.get("opponent_proj")
        if mode == "guillotine":
            low = (o.get("teams") or [{}])[-1]
            them_p = low.get("proj")
        margin = (round(mine_p - them_p, 1)
                  if mine_p is not None and them_p is not None else None)
        live = live_scores(lg.name, wk_all) if LIVE else {}
        my_live = them_live = None
        if live:
            # Only claim a score once one of that league's starters is on the
            # field; otherwise the whole league reads 0.0 as though everyone
            # blanked, when in fact nothing has begun.
            # Gate EACH SIDE separately. Keying both off your own players meant
            # an opponent who was already playing showed nothing.
            def _side(tid):
                if not tid:
                    return None
                tms = (r.get("_starter_teams") or {}).get(tid) or []
                if not any(ls_mod.has_played(x, KST) for x in tms):
                    return None
                return (live.get(tid) or {}).get("total")
            my_live = _side(r.get("_my_id"))
            them_live = _side(r.get("_opp_id"))
        f = _flags(r, lg.name)
        total_flags += len(f)
        if f:
            todo.append((lg.name, f))
        cards.append((lg.name, head, sub, extra, opp,
                      (mine_p, them_p, margin, len(f), my_live, them_live)))

    # --- odds, one card per league ---
    html = ['<div class="home"><div class="ffwrap">',
            '<h2>Where you stand</h2>',
            f'<div class="ffsub">week {wk_all} · five leagues</div>',
            '<table class="fftable"><tr><th>League</th><th>This week</th>'
            '<th></th>'
            + ('<th class="ffnum">Current</th><th class="ffnum">Opp now</th>'
               if LIVE else '')
            + '<th class="ffnum">Pregame</th><th class="ffnum">Them</th>'
            '<th class="ffnum">Margin</th><th>Season</th>'
            '<th class="ffnum">To do</th></tr>']
    for nm, head, sub, extra, opp, nums in cards:
        if nums is None:
            html.append(f'<tr><td><b>{nm}</b></td><td class="dim" '
                        f'colspan="{9 if LIVE else 7}">{sub}</td></tr>')
            continue
        mine_p, them_p, margin, nf, my_live, them_live = nums
        try:
            pct = float(str(head).rstrip("%"))
        except ValueError:
            pct = 50.0
        cls = "good" if pct >= 55 else ("bad" if pct < 45 else "warn")
        mcls = "good" if (margin or 0) > 0 else ("bad" if (margin or 0) < 0 else "dim")
        todo_cell = (f'<span class="bad">{nf}</span>' if nf
                     else '<span class="good">clear</span>')
        html.append(
            f'<tr><td><b>{nm}</b></td>'
            f'<td class="{cls}" style="font-size:19px;font-weight:700">{head}</td>'
            f'<td class="dim" style="font-size:12px">{sub}<br>{opp}</td>'
            + (f'<td class="ffnum" style="font-size:17px;font-weight:700">'
               f'{"\u2014" if my_live is None else f"{my_live:.1f}"}</td>'
               f'<td class="ffnum dim">'
               f'{"\u2014" if them_live is None else f"{them_live:.1f}"}</td>'
               if LIVE else '')
            + f'<td class="ffnum dim">{"" if mine_p is None else f"{mine_p:.1f}"}</td>'
            f'<td class="ffnum dim">{"" if them_p is None else f"{them_p:.1f}"}</td>'
            f'<td class="ffnum {mcls}">'
            f'{"" if margin is None else f"{margin:+.1f}"}</td>'
            f'<td class="dim" style="font-size:12px">{extra}</td>'
            f'<td class="ffnum">{todo_cell}</td></tr>')
    html.append('</table></div></div>')
    st.markdown("\n".join(html), unsafe_allow_html=True)

    # --- what needs you ---
    st.markdown("")
    st.markdown('<div class="home">', unsafe_allow_html=True)
    st.markdown(f"#### Needs you  ·  {total_flags}" if total_flags
                else "#### Needs you")
    st.caption(URGENT_HELP)
    if not todo:
        st.markdown('<div class="home"><div class="ffok">Nothing needs a '
                    'decision. Every lineup is legal, everyone starting is '
                    'playing, and none of them can be improved.</div></div>',
                    unsafe_allow_html=True)
    for nm, items in todo:
        st.markdown(f"**{nm}**")
        for it in items:
            (st.error if it["sev"] == 0 else st.warning)(it["text"])
    st.markdown('</div>', unsafe_allow_html=True)
    st.stop()

if nav == "Sunday":
    from ff import live as live_mod
    st.header("Sunday")
    c1, c2 = st.columns([1, 4])
    wk = int(c1.number_input("Week", 1, 18, _default_week(), key="sun_wk"))
    if c2.button("Refresh now", key="sun_refresh"):
        _games.clear(); _holdings.clear()
    st.caption("Scores refresh every 30 seconds. Your players are pulled from "
               "every league that has drafted, so one game can matter to you "
               "several times over.")

    try:
        gs = _games(wk)
        hold = _holdings(wk)
    except Exception as e:
        st.error(f"couldn't load live data: {type(e).__name__}: {e}")
        gs, hold = [], {}

    if not hold:
        st.info("No drafted leagues yet — nothing to follow.")

    try:
        slots = live_mod.watch_by_slot(gs, hold)
    except Exception:
        slots = []
    if slots:
        st.subheader("What to put on")
        st.caption("One pick per time slot — you can only watch one game at a "
                   "time. Starters only: a bench player's points aren't yours "
                   "this week. A player you roster in three leagues counts "
                   "three times.")
        for s_ in slots:
            g = s_["pick"]
            with st.container(border=True):
                st.markdown(
                    f"**{s_['slot']}** — **{g['away']} @ {g['home']}** on "
                    f"**{g['network']}** · {g['n_players']} starter"
                    f"{'s' if g['n_players'] != 1 else ''}, "
                    f"{g['proj_total']} projected")
                st.dataframe(
                    [{"player": h["player"], "pos": h["position"],
                      "proj": h["proj"], "league": h["league"],
                      "team": h["nfl_team"]} for h in g["players"]],
                    hide_index=True, width="stretch")
                if s_["others"]:
                    with st.expander(f"other games this slot ({len(s_['others'])})"):
                        st.dataframe(
                            [{"game": f"{o['away']} @ {o['home']}",
                              "on": o["network"], "starters": o["n_players"],
                              "proj": o["proj_total"]} for o in s_["others"]],
                            hide_index=True, width="stretch")

    st.subheader("Scoreboard")
    live_now = [g for g in gs if g["state"] == "in"]
    for g in (live_now or gs):
        mine_here = hold.get(g["home"], []) + hold.get(g["away"], [])
        if not mine_here and g["state"] != "in":
            continue
        head_ = (f"**{g['away']} {g['away_score']} — {g['home_score']} {g['home']}**"
                 if g["state"] != "pre" else
                 f"**{g['away']} @ {g['home']}**")
        st.markdown(f"{head_}  ·  {g['detail']}  ·  {g['network']}")
        if mine_here:
            st.dataframe(
                [{"player": h["player"], "pos": h["position"],
                  "league": h["league"],
                  "role": "START" if h["starter"] else "bench",
                  "proj": h["proj"]} for h in
                 sorted(mine_here, key=lambda h: (not h["starter"], -h["proj"]))],
                hide_index=True, width="stretch")
        st.divider()
    st.stop()


# Keepers only exists where the league actually has them, so four of five
# leagues stop carrying a tab that could only ever say "not configured".
HAS_KEEPERS = bool((cfg.get("keepers") or {}).get("max"))
_names = ["Report", "Lineup", "Matchups", "Waivers", "Trades"]
if HAS_KEEPERS:
    _names.append("Keepers")
_T = dict(zip(_names, st.tabs(_names)))


t_report, t_lineup, t_match = _T.get("Report"), _T.get("Lineup"), _T.get("Matchups")
t_waiver, t_trade, t_keep = _T.get("Waivers"), _T.get("Trades"), _T.get("Keepers")


# ---------------------------------------------------------------------------
# Sunday + Watch are cross-league on purpose: your Sunday isn't organised by
# league, it's organised by which games are on.
# ---------------------------------------------------------------------------
with t_report:
    week_now = _default_week()
    st.markdown(th.css(scope="lg"), unsafe_allow_html=True)
    rep = build_report(L.name, week_now)

    if not rep.get("drafted"):
        st.info("No roster yet — this fills in the moment the draft finishes.")
    else:
        o = rep.get("odds") or {}
        sea = rep.get("season_odds") or {}
        d = rep.get("diff") or {}
        mode = o.get("mode")

        if mode == "guillotine":
            head, sub = f"{o['advance']*100:.0f}%", "safe this week"
            opp_line = "guillotine — lowest scorer is eliminated"
            season_v = (f"{sea['advance']*100:.0f}%"
                        if sea.get("advance") is not None else "—")
            season_k = "win the league"
        elif mode == "head_to_head" and o.get("win") is not None:
            head, sub = f"{o['win']*100:.0f}%", "to win"
            opp_line = (f"vs {o['opponent']} · they project "
                        f"{o.get('opponent_proj')}")
            season_v = (f"{sea['playoff']*100:.0f}%"
                        if sea.get("playoff") is not None else "—")
            season_k = f"make playoffs (top {rep.get('playoff_cut') or '?'})"
        else:
            head, sub, opp_line = "—", "", ""
            season_v, season_k = "—", "season"
        try:
            pct = float(head.rstrip("%"))
            hcls = "good" if pct >= 55 else ("bad" if pct < 45 else "warn")
        except ValueError:
            hcls = "dim"

        # If a platform will not tell us what you actually started, show the
        # optimal lineup and say so, rather than rendering an empty table and a
        # meaningless "left on bench" gap.
        have_actual = bool(d.get("actual"))
        if not have_actual:
            d = {**d,
                 "actual": [{**p_, "slot": sl}
                            for sl, v in rep["filled"].items() for p_ in v],
                 "actual_total": rep["total"], "swaps": [], "broken": []}
        gap = d.get("optimal_total", 0) - d.get("actual_total", 0)
        gcls = "bad" if gap > 0.05 else "good"
        blocks = [
            f'<div class="lg"><div class="ffwrap">',
            f'<h2>{L.name} · week {week_now}</h2>',
            f'<div class="ffsub">{opp_line}</div>',
            '<div class="ffrow">',
            f'<div class="ffstat"><div class="v {hcls}">{head}</div>'
            f'<div class="k">{sub}</div></div>',
            f'<div class="ffstat"><div class="v">{d.get("actual_total", 0):.1f}'
            f'</div><div class="k">your lineup</div></div>',
            f'<div class="ffstat"><div class="v {gcls}">{gap:+.1f}</div>'
            f'<div class="k">left on bench</div></div>',
            f'<div class="ffstat"><div class="v">{season_v}</div>'
            f'<div class="k">{season_k}</div></div>',
            '</div>',
        ]
        # NOT `rows` -- that name holds the board rows for the whole script,
        # and shadowing it here broke every tab rendered after this one.
        trows = []
        for a in d.get("actual", []):
            stt = (a.get("status") or "ok")
            pill = ("" if stt == "ok" else
                    f'<span class="pill {"out" if stt in ("out", "unknown") else "risk"}">'
                    f'{stt}</span>')
            note = f'<span class="dim"> {a.get("why", "")}</span>' if a.get("why") else ""
            pcls = "bad" if (a.get("week_points") or 0) <= 0 else ""
            trows.append(
                f'<tr><td class="ffslot">{a.get("slot", "")}</td>'
                f'<td><b>{a["name"]}</b> {pill}{note}</td>'
                f'<td class="dim">{a.get("opponent", "")}</td>'
                f'<td class="ffnum {pcls}">{a.get("week_points", 0):.1f}</td></tr>')
        blocks.append(
            '<table class="fftable"><tr><th>Slot</th><th>Player</th>'
            '<th>Opp</th><th class="ffnum">Pregame</th></tr>'
            + "".join(trows) + '</table>')

        if d.get("swaps") or d.get("broken"):
            blocks.append('<div style="margin-top:18px">')
            for b in d.get("broken", []):
                blocks.append(
                    f'<div class="ffalert"><b>{b.get("slot") or ""}: '
                    f'{b["name"]}</b> is {(b.get("status") or "").upper()}'
                    + (f" — {b['why']}" if b.get("why") else "")
                    + f'. That slot scores {b.get("week_points", 0):.1f}.</div>')
            for sw in d.get("swaps", []):
                blocks.append(
                    f'<div class="ffalert">Bench <b>{sw["out"]}</b> '
                    f'({sw["out_pts"]}) → start <b>{sw["in"]}</b> '
                    f'({sw["in_pts"]}) · <span class="good">'
                    f'{sw["gain"]:+.1f} pts</span></div>')
            blocks.append('</div>')
        else:
            blocks.append('<div style="margin-top:18px"><div class="ffok">'
                          'Lineup is already optimal.</div></div>')
        blocks.append('</div></div>')
        st.markdown("".join(blocks), unsafe_allow_html=True)
        if not have_actual:
            st.caption(f"⚠ {L.platform} did not return a starting lineup, so "
                       "the table above is the OPTIMAL lineup, not what you "
                       "have set.")

        st.subheader("Waivers")
        if not rep["live"]:
            st.caption(f"⚠ No 2026 game data yet — usage over "
                       f"**{rep['season']} through week {rep['through']}**.")
        if rep["cands"] and "error" in rep["cands"][0]:
            st.error(f"usage data unavailable — {rep['cands'][0]['error']}")
        elif not rep["cands"]:
            st.success("Nothing on the wire improves your starting lineup.")
        else:
            st.dataframe(pd.DataFrame(rep["cands"]).drop(columns=["_g"]),
                         hide_index=True, width="stretch")
        if rep.get("risers"):
            with st.expander("Usage risers — speculative adds"):
                st.dataframe(pd.DataFrame(rep["risers"]).drop(columns=["_d"]),
                             hide_index=True, width="stretch")


# ---- Home -----------------------------------------------------------------
# The landing page answers two questions and nothing else: where do I stand in
# each league, and is there anything I have to do about it. Everything deeper
# lives behind a tab.
with t_lineup:
    st.header("Set your lineup")
    from ff import lineup as lineup_mod
    wk = int(st.slider("Week", 1, 18, _default_week(), key="lp_wk"))
    blob = get_blob()
    by_key = {key(r["name"], r["position"]): r for r in rows}
    _teams = rosters_mod.all_teams(L, wk, blob)
    _me = next((t for t in _teams if t.mine), None)
    mine = (lineup_mod.build_pool(L, _me.players, wk, by_key, blob)
            if _me else [])

    if not mine:
        st.info("No roster yet — this fills in after your draft.")
    else:
        st.markdown(th.css(scope="lp"), unsafe_allow_html=True)
        filled, bench = lineup_mod.optimize(mine, L)
        diff = lineup_mod.actual_vs_optimal(mine, filled,
                                            (_me.starters if _me else []))
        have_actual = bool(diff["actual"])
        shown = (diff["actual"] if have_actual
                 else [{**q, "slot": sl} for sl, v in filled.items() for q in v])
        cur = diff["actual_total"] if have_actual else diff["optimal_total"]
        gap = diff["optimal_total"] - cur

        LIVE = games_started(wk)
        KSTATE = kickoff_states(wk)
        PROG = game_progress(wk)
        lv = live_scores(L.name, wk) if LIVE else {}
        my_lv = (lv.get(_me.team_id) if _me else None) or {}
        got = my_lv.get("starters") or {}
        if LIVE:
            m = st.columns(4)
            _any_mine = any(ls_mod.has_played(q.get("team"), KSTATE) for q in shown)
            m[0].metric("Current",
                        f"{my_lv.get('total', 0):.1f}" if _any_mine else "—",
                        (f"{my_lv.get('total', 0) - cur:+.1f} vs pregame"
                         if _any_mine else "nobody has kicked off"))
            m[1].metric("Pregame projection", f"{cur:.1f}")
            m[2].metric("Optimal pregame", f"{diff['optimal_total']:.1f}")
            m[3].metric("Leaving on bench", f"{gap:+.1f}", delta_color="inverse")
        else:
            m = st.columns(3)
            m[0].metric("Pregame projection", f"{cur:.1f}")
            m[1].metric("Optimal pregame", f"{diff['optimal_total']:.1f}")
            m[2].metric("Leaving on bench", f"{gap:+.1f}", delta_color="inverse")

        # ONE table. There were two -- "Start" and "Bench" -- which meant
        # reading both and diffing them yourself to find the problem. The
        # instruction now sits on the row it applies to.
        fix = {sw["out"]: sw for sw in diff["swaps"]}
        trows = []
        for a_ in shown:
            nm_ = a_["name"]
            sw = fix.get(nm_)
            stt = (a_.get("status") or "ok")
            bad_row = stt in ("out", "unknown") or (a_.get("week_points") or 0) <= 0
            note, cls = "", ""
            if sw:
                cls = "bad"
                why = f'{stt.upper()}' + (f' · {a_.get("why")}' if a_.get("why") else "")
                note = (f'<span class="bad">{why} — start <b>{sw["in"]}</b> '
                        f'({sw["in_pts"]}) · {sw["gain"]:+.1f}</span>'
                        if bad_row else
                        f'<span class="warn">start <b>{sw["in"]}</b> '
                        f'({sw["in_pts"]}) · {sw["gain"]:+.1f}</span>')
            elif stt == "risk":
                note = f'<span class="warn">{a_.get("why", "questionable")}</span>'
            elif bad_row:
                cls = "bad"
                note = f'<span class="bad">{a_.get("why") or "not playing"}</span>'
            live_cell = ""
            if LIVE:
                pts_now = got.get(key(nm_, a_.get("position")))
                proj_now = a_.get("week_points", 0) or 0
                if not ls_mod.has_played(a_.get("team"), KSTATE):
                    # Not kicked off. A zero here would read as "he blanked".
                    pts_now = None
                if pts_now is None:
                    live_cell = ('<td class="ffnum dim">—</td>'
                                 '<td class="ffnum dim">—</td>')
                else:
                    # Each number against the benchmark that means something:
                    # the score against his PACE (how far through his game he
                    # is), the live projection against where he STARTED.
                    fr = ls_mod.remaining(a_.get("team"), PROG)
                    lp = pts_now + proj_now * fr
                    pace = proj_now * (1 - fr)
                    lcls = ("good" if pts_now >= pace
                            else "warn" if pts_now >= pace * .6 else "bad")
                    pcls = ("good" if lp >= proj_now
                            else "warn" if lp >= proj_now * .8 else "bad")
                    live_cell = (f'<td class="ffnum {lcls}" '
                                 f'style="font-size:15px">{pts_now:.1f}</td>'
                                 f'<td class="ffnum {pcls}">{lp:.1f}</td>')
            trows.append(
                f'<tr style="{"background:rgba(248,113,113,.07)" if cls else ""}">'
                f'<td class="ffslot">{a_.get("slot", "")}</td>'
                f'<td class="{cls}"><b>{nm_}</b></td>'
                f'<td class="dim">{a_.get("team") or ""}</td>'
                f'<td class="dim">{a_.get("opponent", "")}</td>'
                + live_cell
                + f'<td class="ffnum {"dim" if LIVE else cls}">'
                  f'{a_.get("week_points", 0):.1f}</td>'
                  f'<td>{note}</td></tr>')
        st.markdown(
            '<div class="lp"><div class="ffwrap"><table class="fftable">'
            '<tr><th>Slot</th><th>Player</th><th>Tm</th><th>Opp</th>'
            + ('<th class="ffnum">Scored</th><th class="ffnum">Proj now</th>'
               if LIVE else '')
            + '<th class="ffnum">Pregame</th><th>Action needed</th></tr>'
            + "".join(trows) + '</table></div></div>', unsafe_allow_html=True)
        if not have_actual:
            st.caption(f"⚠ {L.platform} returned no starting lineup, so this is "
                       "the OPTIMAL lineup, not what you have set.")

        with st.expander(f"Bench ({len(bench)})"):
            st.dataframe(pd.DataFrame([{
                "Player": q["name"], "Pos": q.get("pos_rank", q["position"]),
                "Tm": q.get("team") or "", "Opp": q.get("opponent", ""),
                "Proj": q.get("week_points", 0),
                "Status": "" if q.get("status") == "ok"
                          else (q.get("status") or "").upper(),
            } for q in sorted(bench, key=lambda x: -x.get("week_points", 0))]),
                hide_index=True, width="stretch")

        calls = lineup_mod.close_calls(filled, bench, L)
        if calls:
            st.caption("**Too close to call:** " + " · ".join(
                f"{c['slot']} — {c['starting']['name']} over "
                f"{c['alternative']['name']} (gap {c['gap']})" for c in calls[:4])
                + " — inside a projection's noise; use your own read.")


# ---- Matchups -------------------------------------------------------------
with t_match:
    st.header("Around the league")
    wkm = _default_week()
    blob = get_blob()
    by_key = {key(r["name"], r["position"]): r for r in rows}
    tms = rosters_mod.all_teams(L, wkm, blob)
    if not rosters_mod.has_drafted(L, tms):
        st.info("Not drafted yet.")
    else:
        st.markdown(th.css(scope="mu"), unsafe_allow_html=True)
        wpm = lineup_mod.weekly_points(wkm, L.scoring)
        # Must use the SAME model as Home, or the two tabs disagree about who
        # is bottom of the league: Home was already live while this was still
        # pregame, so a team whose players had underperformed still showed its
        # Saturday number here.
        PROGM = game_progress(wkm)
        _lv_any = any(v < 1.0 for v in (PROGM or {}).values())
        mlv = live_scores(L.name, wkm) if _lv_any else {}
        proj = {}
        for t in tms:
            if _lv_any:
                mu, sd, _st = winprob_mod.live_project_team(
                    t, wpm, L, mlv.get(t.team_id), PROGM, week=wkm)
            else:
                mu, sd, _st = winprob_mod.project_team(t, wpm, L, week=wkm)
            proj[t.team_id] = (t, mu, sd)
        pairs = rosters_mod.all_matchups(L, wkm, tms)
        MLIVE = games_started(wkm)
        MST = kickoff_states(wkm)

        if not pairs:
            # Guillotine: no matchups, so the league IS the scoreboard and the
            # only thing that matters is distance from the bottom.
            st.caption("Guillotine — no matchups. The lowest score is "
                       "eliminated, so this is the whole field ranked, and the "
                       "gap to the bottom is your real margin.")
            order = sorted(proj.values(), key=lambda x: -x[1])
            low = order[-1][1]
            rws = []
            for i, (t, mu, sd) in enumerate(order, 1):
                me_ = t.mine
                cls = "good" if me_ else ""
                marg = mu - low
                mcls = ("bad" if marg < 8 else "warn" if marg < 20 else "good")
                rws.append(
                    f'<tr style="{"background:rgba(74,222,128,.06)" if me_ else ""}">'
                    f'<td class="dim">{i}</td>'
                    f'<td class="{cls}"><b>{t.name}</b>'
                    f'{" ← you" if me_ else ""}</td>'
                    f'<td class="ffnum">{mu:.1f}</td>'
                    f'<td class="ffnum dim">±{sd:.0f}</td>'
                    f'<td class="ffnum {mcls}">{marg:+.1f}</td></tr>')
            st.markdown('<div class="mu"><div class="ffwrap"><table class="fftable">'
                        '<tr><th>#</th><th>Team</th><th class="ffnum">Pregame</th>'
                        '<th class="ffnum">±</th>'
                        '<th class="ffnum">vs last</th></tr>'
                        + "".join(rws) + '</table></div></div>',
                        unsafe_allow_html=True)
        else:
            rws = []
            for h, a_ in pairs:
                if h not in proj or a_ not in proj:
                    continue
                th_, hm, hs = proj[h]
                ta_, am, asd = proj[a_]
                pw = winprob_mod.head_to_head((hm, hs), (am, asd))
                def _live_total(t):
                    if not any(ls_mod.has_played(q.get("team"), MST)
                               for q in t.players):
                        return None
                    return (mlv.get(t.team_id) or {}).get("total")
                hl, al = _live_total(th_), _live_total(ta_)
                mine_here = th_.mine or ta_.mine
                hi = "background:rgba(74,222,128,.06)" if mine_here else ""
                hcls = "good" if pw >= .5 else "bad"
                acls = "good" if pw < .5 else "bad"
                rws.append(
                    f'<tr style="{hi}">'
                    f'<td class="{"good" if th_.mine else ""}"><b>{th_.name}</b>'
                    f'{" ← you" if th_.mine else ""}</td>'
                    + (f'<td class="ffnum" style="font-size:16px;font-weight:700">'
                       f'{hl:.1f}</td>' if hl is not None else
                       ('<td class="ffnum dim">—</td>' if MLIVE else ''))
                    + f'<td class="ffnum dim">{hm:.1f}</td>'
                    f'<td class="ffnum {hcls}">{pw*100:.0f}%</td>'
                    f'<td class="dim" style="text-align:center">vs</td>'
                    f'<td class="ffnum {acls}">{(1-pw)*100:.0f}%</td>'
                    + f'<td class="ffnum dim">{am:.1f}</td>'
                    + (f'<td class="ffnum" style="font-size:16px;font-weight:700">'
                       f'{al:.1f}</td>' if al is not None else
                       ('<td class="ffnum dim">—</td>' if MLIVE else ''))
                    + f'<td class="{"good" if ta_.mine else ""}"><b>{ta_.name}</b>'
                    f'{" ← you" if ta_.mine else ""}</td></tr>')
            st.markdown('<div class="mu"><div class="ffwrap"><table class="fftable">'
                        + ('<tr><th>Home</th>'
                           + ('<th class="ffnum">Current</th>' if MLIVE else '')
                           + '<th class="ffnum">Pregame</th>'
                             '<th class="ffnum">Win</th><th></th>'
                             '<th class="ffnum">Win</th><th class="ffnum">Pregame</th>'
                           + ('<th class="ffnum">Current</th>' if MLIVE else '')
                           + '<th>Away</th></tr>')
                        + "".join(rws) + '</table></div></div>',
                        unsafe_allow_html=True)
            st.caption("Every game this week, scored under this league's rules. "
                       "Odds come from the same distribution as your own "
                       "matchup, so they are directly comparable. **Pregame** is "
                       "the projection published before kickoff — it does NOT "
                       "decay as games play, so mid-Sunday read it as where the "
                       "week started, not where it is heading.")

        st.subheader("Where you stand")
        order = sorted(proj.values(), key=lambda x: -x[1])
        mi_ = next((i for i, (t, *_r) in enumerate(order, 1) if t.mine), None)
        med = order[len(order) // 2][1]
        c = st.columns(3)
        c[0].metric("Projected rank", f"{mi_} of {len(order)}")
        c[1].metric("League median", f"{med:.1f}")
        mine_mu = next((mu for t, mu, _s in proj.values() if t.mine), 0)
        c[2].metric("You vs median", f"{mine_mu - med:+.1f}")


# ---- Waivers --------------------------------------------------------------
with t_waiver:
    st.header("Waivers")
    st.caption(f"**{L.waiver_note}.** No FAAB in any of your leagues — the cost of a "
               "claim is your waiver position, so the call is claim, wait, or skip.")

    if False:   # cross-platform now; kept as a switch if a platform regresses
        st.info("Waiver analysis needs rosters this platform isn't returning. "
                "ESPN wiring is still to do.")
    else:
        week = int(st.slider("Week", 1, 18, _default_week(), key="wv_wk"))
        season, through, live_data = trend_season(week)
        replay = not live_data          # kept: claim_call and heat_rank read it
        if not live_data:
            st.caption(f"⚠ No 2026 game data yet — usage measured over "
                       f"**{season} through week {through}**. Rosters below are "
                       "live. Switches itself once games are played.")

        with st.spinner("crunching usage trends…"):
            blob = get_blob()
            trend = weekly_mod.usage_trend(season, through)
            trend["k"] = [key(n, p) for n, p in
                          zip(trend.player_display_name, trend.position)]
            ppg = weekly_mod.recent_points(season, through, L.scoring)
            trend["ppg"] = trend.k.map(ppg).fillna(0.0)

            by_key = {key(r["name"], r["position"]): r for r in rows}
            # Cross-platform: who is rostered anywhere, and which are mine.
            all_t = rosters_mod.all_teams(L, week, blob)
            taken = {key(pl["name"], pl.get("position"))
                     for t in all_t for pl in t.players}
            mine = []
            for t in all_t:
                if not t.mine:
                    continue
                for pl in t.players:
                    row = by_key.get(key(pl["name"], pl.get("position")))
                    if row:
                        mine.append(row)

            heat = {} if replay else weekly_mod.heat_rank(blob)
            base = draft_mod.lineup_value(mine, L, L.replacement)
            cands = []
            for r in trend.itertuples():
                if r.k in taken or r.snap_pct_recent <= 0.25:
                    continue
                row = by_key.get(r.k)
                if row is None:
                    continue
                gain = draft_mod.lineup_value(mine + [row], L, L.replacement) - base
                call, why = weekly_mod.claim_call(gain, heat.get(r.k),
                                                  L.waiver_style, not replay)
                cands.append({"Player": row["name"], "Pos": row["pos_rank"],
                              "Adds": round(gain), "PPG": round(r.ppg, 1),
                              "Snap%": f"{r.snap_pct_recent*100:.0f}%",
                              "ΔSnap": f"{r.snap_delta*100:+.0f}",
                              "Tgt": round(r.targets_recent, 1),
                              "ΔTgt": f"{r.tgt_delta:+.1f}",
                              "Call": call, "Why": why, "_g": gain})

        useful = sorted([c for c in cands if c["_g"] > 0], key=lambda c: -c["_g"])
        st.subheader("Improves your starting lineup")
        if not useful:
            st.info("**Nothing on the wire cracks your starting lineup** — with a "
                    "full healthy roster that is the normal answer, since a "
                    "player who doesn't start adds zero by definition. The best "
                    "available are listed below anyway: they are who you'd take "
                    "if an injury opened a slot.")
        else:
            st.dataframe(pd.DataFrame(useful).drop(columns=["_g"]),
                         hide_index=True, width='stretch')

        # Best available regardless of whether they crack the lineup. Ranked by
        # season value, because that is what makes someone worth a bench spot
        # once weekly lineup gain has flatlined to zero for everyone.
        st.subheader("Best available")
        _free = [r for r in rows
                 if key(r["name"], r["position"]) not in taken
                 and r.get("pos_rank_n") is not None]
        _free.sort(key=lambda r: -r["points"])
        _base = draft_mod.lineup_value(mine, L, L.replacement)
        st.dataframe(pd.DataFrame([{
            "Player": r["name"], "Pos": r["pos_rank"],
            "Team": r.get("team") or "",
            "Season": round(r["points"]),
            "VORP": round(r["vorp"]),
            "Adds now": round(draft_mod.lineup_value(mine + [r], L, L.replacement)
                              - _base, 1),
        } for r in _free[:20]]), hide_index=True, width="stretch")
        st.caption("**Adds now** is what he'd add to THIS week's starting "
                   "lineup; **Season** and **VORP** are why he's worth a roster "
                   "spot even when that is zero.")

        st.subheader("Usage risers — opportunity moves before production")
        movers = sorted([c for c in cands], key=lambda c: -float(c["ΔSnap"]))[:12]
        st.dataframe(pd.DataFrame(movers)[
            ["Player", "Pos", "Snap%", "ΔSnap", "Tgt", "ΔTgt", "PPG"]],
            hide_index=True, width='stretch')

        if L.raw.get("guillotine") and L.waiver_style == "faab":
            st.divider()
            st.subheader("What to bid")
            bud = faab_balance(L.name) or 0
            wl = max(1, regular_weeks(L.name) - week + 1)
            rep_ = build_report(L.name, week)
            sea_ = rep_.get("season_odds") or {}
            alive = max(1.0, sea_.get("weeks_survived", wl))
            c1, c2, c3 = st.columns(3)
            c1.metric("FAAB left", f"${bud}")
            c2.metric("Per calendar week", f"${bud/wl:.0f}", f"{wl} weeks left")
            c3.metric("Per week you're alive", f"${bud/alive:.0f}",
                      f"~{alive:.1f} weeks", delta_color="off")
            st.info(
                f"**Your money is worthless the moment you are eliminated**, so "
                f"the horizon is the **{alive:.1f} weeks you expect to survive**, "
                f"not the {wl} on the calendar. Dividing by the calendar "
                f"under-bids by **{(bud/alive)/(bud/wl):.1f}x**. When a team goes "
                "out, its whole roster hits the wire at once — that is the "
                "cheapest good player you will see all season, and the week to "
                "spend.")
            with st.spinner("pricing the wire…"):
                from bakeoff import weekly_score as _wsc
                from ff import faab as faab_mod
                helpers = [c for c in cands if c.get("_g", 0) > 0][:6]
                priced = []
                for c in helpers:
                    row = by_key.get(key(c["Player"], c["Pos"][:2]
                                         if c["Pos"][:2] in ("QB", "RB", "WR", "TE")
                                         else c["Pos"]))
                    if row is None:
                        continue
                    v = faab_mod.guillotine_value(
                        all_t, by_key, L, byes_all(), _wsc, row, bud, wl,
                        sims=1500)
                    priced.append({"Player": v["player"], "Pos": row["pos_rank"],
                                   "+weeks alive": v["extra_weeks"],
                                   "Fair bid": f"${v['fair']}",
                                   "Max": f"${v['max']}",
                                   "Rivals wanting him": v["rivals_who_want_him"]})
            if priced:
                st.dataframe(pd.DataFrame(priced), hide_index=True, width="stretch")
                st.caption(
                    "Priced in **weeks of survival**, not points: run the season "
                    "with and without him and difference the expected weeks alive. "
                    "If he buys a fifth more season, he is worth a fifth of the "
                    "budget. **Fair** is your valuation; **Max** adds a premium "
                    "for how many rivals also need the position. A player who "
                    "doesn't crack your starting lineup buys zero weeks and is "
                    "worth $0 however famous he is.")
            else:
                st.caption("Nothing on the wire improves your starting lineup, so "
                           "nothing is worth a bid yet. This fills in the week a "
                           "team is eliminated and its roster is released.")

        if mine:
            st.subheader("Droppable")
            drops = sorted(mine, key=lambda r: r["vorp"])[:5]
            st.dataframe(pd.DataFrame([{
                "Player": r["name"], "Pos": r["pos_rank"], "VORP": round(r["vorp"]),
                "Note": "below replacement" if r["vorp"] < 0 else "lowest value rostered",
            } for r in drops]), hide_index=True, width='stretch')


# ---- Trades ---------------------------------------------------------------
with t_trade:
    st.header("Trades")
    if False:   # cross-platform now
        st.info("Trade search needs rosters this platform isn't returning.")
    else:
        blob = get_blob()
        by_key = {key(r["name"], r["position"]): r for r in rows}
        mine, others = rosters_mod.board_rosters(L, by_key, 1, blob)
        if not mine:
            st.info("No roster yet — this fills in after your draft.")
        else:
            sur = trade_mod.surplus(mine, L)
            cols = st.columns(len(sur))
            for col, (pos, s) in zip(cols, sur.items()):
                label = "hole" if s["hole"] > 15 else ("depth" if s["depth"] > 25 else "set")
                col.metric(pos, label, f"{s['n']} rostered / {s['starts']} start")

            st.subheader("Likely to be accepted")
            st.caption("Scored by change in **projected starting lineup** for both teams — "
                       "bench points don't count. *Optics* is the raw value differential "
                       "the other manager sees; very negative reads as a fleece and "
                       "gets declined however sound it is.")
            fair = trade_mod.find(mine, others, L, min_my_gain=8, min_their_gain=8,
                                  rank="fair", limit=8)
            for t in fair:
                g = " + ".join(f"{p['name']} ({p['pos_rank']})" for p in t["give"])
                r = " + ".join(f"{p['name']} ({p['pos_rank']})" for p in t["get"])
                with st.container(border=True):
                    st.markdown(f"**{t['team']}** — send **{g}** → get **{r}**")
                    m = st.columns(4)
                    m[0].metric("You", f"{t['my_delta']:+.0f}")
                    m[1].metric("Them", f"{t['their_delta']:+.0f}")
                    m[2].metric("Optics", f"{t['optics']:+.0f}")
                    m[3].caption(trade_mod.verdict(t))

            with st.expander("Aggressive asks (best case if they say yes)"):
                agg = trade_mod.find(mine, others, L, min_my_gain=8, min_their_gain=3,
                                     rank="mine", limit=5)
                for t in agg:
                    g = " + ".join(p["name"] for p in t["give"])
                    r = " + ".join(p["name"] for p in t["get"])
                    st.markdown(f"- **{t['team']}**: send {g} → get {r} · "
                                f"you {t['my_delta']:+.0f}, them {t['their_delta']:+.0f}, "
                                f"optics {t['optics']:+.0f} — _{trade_mod.verdict(t)}_")

            st.subheader("Evaluate an offer")
            c1, c2 = st.columns(2)
            them = c1.selectbox("Team", [o["name"] for o in others])
            tp = next(o for o in others if o["name"] == them)
            give = c1.multiselect("They want", [p["name"] for p in mine])
            get = c2.multiselect("They offer", [p["name"] for p in tp["players"]])
            if give and get:
                gv = [p for p in mine if p["name"] in give]
                gt = [p for p in tp["players"] if p["name"] in get]
                res = trade_mod.review(gv, gt, mine, tp["players"], L)
                m = st.columns(3)
                m[0].metric("Your lineup", f"{res['my_delta']:+.0f}")
                m[1].metric("Their lineup", f"{res['their_delta']:+.0f}")
                m[2].metric("Optics", f"{res['optics']:+.0f}")
                (st.success if res["accept"] else st.error)(res["verdict"])


# ---- Keepers --------------------------------------------------------------
if t_keep is not None:          # league has no keepers -> no tab
    with t_keep:
        st.header("Keepers — next season")
        k = cfg.get("keepers") or {}
        if not k or not cfg.get("owner_id"):
            st.info("This league has no keeper settings in leagues.yaml.")
        else:
            esc = k.get("escalation", 1)
            st.caption(f"Keep up to **{k['max']}**. A kept player costs a pick "
                       f"**{esc} round earlier** than where you drafted him — so a "
                       f"round-4 pick keeps for a round-3. Round-1 picks "
                       f"{'are keepable' if k.get('round_one_keepable') else 'are **not** keepable'}"
                       f", since there is no round 0 to escalate into.")
            blob = get_blob()
            try:
                # THIS season's league, not last season's: the question is what you
                # can keep into next year, so the draft that sets the cost is the
                # one that just happened.
                cands = keeper_mod.roster_and_costs(
                    L.league_id, cfg["owner_id"], blob,
                    escalation=esc,
                    undrafted_round=k.get("undrafted_round"),
                    round_one_keepable=k.get("round_one_keepable", True))
            except Exception as e:
                cands = []
                st.error(f"couldn't read the draft: {type(e).__name__}: {e}")
            cands = [c for c in cands if c["position"] not in ("K", "DEF", "DST")]
            if cands:
                res = keeper_mod.value(cands, rows, L)
                keep = [r for r in res if (r.get("surplus") or 0) > 0][: k["max"]]
                if keep:
                    st.success("**Keep:** " + " · ".join(
                        f"{r['name']} (rd{r['cost_round']}, {r['surplus']:+.0f})"
                        for r in keep))
                    if len(keep) < k["max"]:
                        st.warning(f"Only {len(keep)} of {k['max']} clear zero. Don't "
                                   "fill the rest — keeping a negative-surplus "
                                   "player is worse than using the pick.")
                else:
                    st.info("Nobody is worth his keeper cost. Draft clean.")
                st.markdown(th.css(scope="kp"), unsafe_allow_html=True)
                krows = []
                for r in res:
                    on = r in keep
                    dr = r.get("drafted_round")
                    sur = r.get("surplus")
                    scls = "good" if (sur or 0) > 0 else "bad"
                    krows.append(
                        f'<tr style="{"background:rgba(74,222,128,.06)" if on else ""}">'
                        f'<td>{"✓" if on else ""}</td>'
                        f'<td><b>{r["name"]}</b></td>'
                        f'<td class="dim">{r.get("pos_rank", r["position"])}</td>'
                        f'<td class="ffnum dim">{("rd" + str(dr)) if dr else "undrafted"}</td>'
                        f'<td class="ffnum">'
                        f'{("rd" + str(r["cost_round"])) if r.get("cost_round") else "—"}</td>'
                        f'<td class="ffnum">'
                        f'{round(r["vorp"]) if r.get("vorp") is not None else ""}</td>'
                        f'<td class="ffnum {scls}">'
                        f'{round(sur) if sur is not None else ""}</td>'
                        f'<td class="dim">{r.get("note", "")}</td></tr>')
                st.markdown(
                    '<div class="kp"><div class="ffwrap"><table class="fftable">'
                    '<tr><th></th><th>Player</th><th>Pos</th>'
                    '<th class="ffnum">Drafted</th><th class="ffnum">Keeps at</th>'
                    '<th class="ffnum">VORP</th><th class="ffnum">Surplus</th>'
                    '<th>Note</th></tr>' + "".join(krows) + '</table></div></div>',
                    unsafe_allow_html=True)
                st.caption("**Keeps at** is the round it costs you next year. "
                           "**Surplus** is his value minus what you'd expect from "
                           "that pick — positive means keeping beats drafting.")
