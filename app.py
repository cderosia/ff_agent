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
from ff import special as special_mod    # noqa: E402
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
from ff import lineup as lineup_mod        # noqa: E402
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
    """League settings, cached -- but NEVER a partial load.

    A transient DNS blip made four of five leagues fail, and the failure was
    then cached for fifteen minutes: the sidebar lost those leagues and kept
    losing them long after the network recovered. A partial result is dropped
    from the cache immediately so the next rerun retries, which costs one
    refetch and avoids a quarter-hour of a half-empty app.
    """
    leagues, errors = load_all()
    if errors:
        get_leagues.clear()
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
    """The week every page opens on.

    Sleeper publishes two of these and they disagree for about a day.
    `display_week` lags on purpose -- it holds on the week you just played
    while Monday night settles -- and `week` advances Tuesday morning. This app
    is for deciding what to do NEXT, and by Tuesday the previous week's results
    are final and waivers are clearing, so it follows `week`.

    Every page defaults from here (Home, Sunday, Lineup, Matchups, Waivers,
    Donuts), so they all turn over together. Each keeps its own week control
    for looking back.
    """
    st_ = nfl_state()
    w = st_.get("week") or st_.get("display_week")
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

# ONE radio. Two radios could not be made mutually exclusive: a Streamlit widget
# keeps its own stored selection, which overrides `index=None`, so the league
# list stayed visually selected while Home was also selected -- and picking the
# league you were already "on" fired no change event, which is why 719 needed a
# detour through another league to open. A single control cannot get into that
# state at all.
NAV = ["Home", "Sunday"]
OPTIONS = NAV + names


def _fmt(o):
    # Leagues indented so they still read as a group under the two views.
    return o if o in NAV else f"   {o}"


view = st.sidebar.radio("View", OPTIONS, index=0, key="view",
                        format_func=_fmt, label_visibility="collapsed")
in_league = view not in NAV
nav = view
choice = view if in_league else (st.session_state.get("last_league") or names[0])
if in_league:
    st.session_state["last_league"] = view
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
    # _default_week(), not display_week: this line names the week every page is
    # actually showing, and the two disagree for about a day after Monday night.
    f"**{_ns.get('season','2026')} week {_default_week()}** "
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
    """Both sides of every league, in ONE roster fetch.

    The Sunday page needs your players and your opponents'; pulling them
    separately would read every roster on every platform twice.
    """
    from ff import live
    from ff.leagues import load_all as _la
    ls, _ = _la()
    return live.holdings(ls, week, get_blob(), sides=("mine", "opp"))


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


def completed_week() -> int:
    """Last week whose games are finished. Playoff odds are keyed on this, so
    they recompute once when the week rolls over (Tuesday, after Monday night)
    and hold steady in between rather than twitching every refresh."""
    w = _default_week()
    try:
        if ls_mod.any_started(_games(w)) and not all(
                g.get("state") == "post" for g in _games(w)):
            return w - 1          # current week still in progress
        return w if _games(w) and all(g.get("state") == "post"
                                      for g in _games(w)) else w - 1
    except Exception:
        return max(0, w - 1)


@st.cache_data(ttl=86400, show_spinner=False)
def standings_for(league_name: str, through: int) -> dict:
    """Records as of a completed week. `through` is part of the cache key, so
    this refetches when the week turns over and not before."""
    L_, _r, _m = hydrate(league_name)
    return rosters_mod.standings(L_)


@st.cache_data(ttl=45, show_spinner=False)
def game_progress(week: int) -> dict:
    """{TEAM: fraction of his game still to play}. Drives live odds."""
    try:
        return ls_mod.game_progress(_games(week))
    except Exception:
        return {}


@st.cache_data(ttl=45, show_spinner=False)
def allowed_now(week: int) -> dict:
    """{TEAM: (points allowed, yards allowed)} for games in progress.

    Only defenses need this, and only while a game is live -- see
    special.dst_live for why a defense's tiers cannot be read off its score.
    """
    try:
        from ff import live as _lv
        return _lv.allowed(_games(week))
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


def report(league_name: str, week: int):
    """build_report, with a cache key that turns over every minute once games
    are live. The 10-minute TTL was right in the week but wrong on a Sunday:
    live odds and live scores inside the report could not move faster than the
    report itself was allowed to be rebuilt."""
    bust = 0
    try:
        if ls_mod.any_started(_games(week)):
            bust = int(time.time() // 60)
    except Exception:
        pass
    return build_report(league_name, week, bust)


@st.cache_data(ttl=600, show_spinner="building your report…")
def build_report(league_name: str, week: int, bust: int = 0):
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
        # The field means the teams still IN it. An eliminated manager keeps
        # his row and loses his roster, so leaving him in compares you against
        # a permanent 0.0 and reports a cushion you do not have.
        odds = winprob_mod.league_odds(
            rosters_mod.active_teams(teams), wp, L_, opponent=opp, week=week,
            live=_livesc if _any_live else None,
            progress=_prog if _any_live else None,
            allowed=allowed_now(week) if _any_live else None)
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
        _tw = regular_weeks(league_name)
        _done = completed_week()
        _stand = standings_for(league_name, _done)
        _recs = [_stand.get(t_.team_id, {}) for t_ in teams]
        _elim = [(r.get("losses", 0) > 0 and L_.raw.get("guillotine"))
                 for r in _recs]
        season_out = winprob_mod.season_odds(
            totals, mi, playoff_count(league_name) or 6,
            weeks=_tw, weeks_left=max(0, _tw - _done),
            records=_recs,
            eliminated=_elim if L_.raw.get("guillotine") else None,
            guillotine=bool(L_.raw.get("guillotine")))
        season_out["through"] = _done
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
        # RISERS come from the usage frame -- a riser needs a baseline to have
        # risen from. CLAIMS come from the board, so a player absent from the
        # usage data (a rookie, or anyone when this falls back to last season)
        # can still be recommended. Keeping claims on the usage frame hid
        # Jadarian Price, worth +32 to the starting lineup, from the Report tab
        # and the Waivers tab at the same time.
        tmap = {r.k: r for r in trend.itertuples()}
        for r in trend.itertuples():
            if r.k in taken or r.snap_pct_recent <= 0.25:
                continue
            row = by_key.get(r.k)
            if row is None:
                continue
            risers.append({"Player": row["name"], "Pos": row["pos_rank"],
                           "PPG": round(r.ppg, 1),
                           "Snap%": f"{r.snap_pct_recent*100:.0f}%",
                           "ΔSnap": f"{r.snap_delta*100:+.0f}",
                           "Tgt": round(r.targets_recent, 1),
                           "_d": r.snap_delta})
        for row in rows_:
            k_ = key(row["name"], row["position"])
            if k_ in taken or row.get("pos_rank_n") is None:
                continue
            gain = draft_mod.lineup_value(mine + [row], L_, L_.replacement) - base
            if gain <= 0:
                continue
            t_ = tmap.get(k_)
            call, why = weekly_mod.claim_call(gain, heat.get(k_),
                                              L_.waiver_style, live)
            cands.append({"Player": row["name"], "Pos": row["pos_rank"],
                          "Adds": round(gain),
                          "PPG": round(t_.ppg, 1) if t_ is not None else 0.0,
                          "Snap%": (f"{t_.snap_pct_recent*100:.0f}%"
                                    if t_ is not None else "—"),
                          "ΔSnap": (f"{t_.snap_delta*100:+.0f}"
                                    if t_ is not None else "—"),
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


LIVEDOT = '<span class="livedot"></span>'


def dot_if_live(team, states) -> str:
    """Green dot when this player's game is in progress."""
    return LIVEDOT if ls_mod.is_playing(team, states) else ""


def team_dot(t, states) -> str:
    """Green dot when any of a fantasy team's STARTERS is on the field."""
    return (LIVEDOT if any(ls_mod.is_playing(q.get("team"), states)
                           for q in (t.starters or [])) else "")


def side_by_side(league, a_team, b_team, week, live, prog, states,
                 by_key, blob, scope="mu") -> str:
    """Two lineups laid out the way a fantasy matchup is normally read.

    Names sit on the outer edges and the numbers meet in the middle around the
    slot, so the two teams mirror each other:

        Jalen Hurts   12.3  18.2 | QB |  9.1  20.4   Dak Prescott

    Per-player numbers come from `lineup.build_pool` -- the SAME function the
    Lineup tab uses -- rather than being recomputed here. The old version read
    `weekly_points` directly, which carries no defenses at all (they come from
    `special.dst_week`), so every DST projected 0.0 and the expander's total
    disagreed with the matchup total by exactly one defense. Projections are
    derived in one place and displayed in many; anywhere that recalculates them
    is a place they can drift.
    """
    def side(t):
        pool = lineup_mod.build_pool(league, t.players, week, by_key, blob)
        idx = {key(q["name"], q["position"]): q for q in pool}
        row = (live or {}).get(t.team_id) or {}
        got = row.get("starters") or {}
        out = []
        for q in (t.starters or []):
            if not q.get("name"):
                out.append(None)
                continue
            pos = (q.get("position") or "").upper()
            pos = "DST" if pos in ("DEF", "D/ST") else pos
            src = idx.get(key(q["name"], pos)) or {}
            pj = src.get("week_points") or 0.0
            tm = src.get("team") or q.get("team")
            sc_ = got.get(key(q["name"], pos))
            sc_ = None if sc_ is None else float(sc_)
            fr = ls_mod.remaining(tm, prog)
            out.append({"slot": q.get("slot"), "name": q["name"], "team": tm,
                        "cur": sc_,
                        "proj": special_mod.live_points(
                            league.scoring, pos, sc_ or 0, pj, fr,
                            ls_mod.pick(tm, allowed_now(week))),
                        "playing": ls_mod.is_playing(tm, states),
                        "started": ls_mod.has_played(tm, states)})
        return out

    A, B = side(a_team), side(b_team)
    a_mine, b_mine = bool(a_team.mine), bool(b_team.mine)

    # Player names are never coloured. Green is semantic everywhere else in the
    # app (ahead / good), so colouring every name on your own team both drowned
    # that meaning and made the column look like a status it wasn't. Whose team
    # is whose is carried by the header and the dot, not by the row text.
    def cells(z, right):
        if not z:
            return '<td colspan="3"></td>'
        cur = "—" if not z["started"] else f'{(z["cur"] or 0):.1f}'
        prj = f'{z["proj"]:.1f}'
        if right:
            # Mirror image: numbers hug the centre, name flush to the right
            # edge, and the dot sits INSIDE the name so the right edge stays
            # flush -- the same reason it trails the name on the left.
            dot = f'<span class="livedot pre"></span>' if z["playing"] else ""
            return (f'<td class="ffnuml">{cur}</td>'
                    f'<td class="ffnuml dim">{prj}</td>'
                    f'<td class="ffright">{dot}{z["name"]}</td>')
        dot = LIVEDOT if z["playing"] else ""
        return (f'<td>{z["name"]}{dot}</td>'
                f'<td class="ffnum">{cur}</td>'
                f'<td class="ffnum dim">{prj}</td>')

    rows = []
    for x, y in zip(A + [None] * max(0, len(B) - len(A)),
                    B + [None] * max(0, len(A) - len(B))):
        slot = (x or y or {}).get("slot", "")
        rows.append('<tr>' + cells(x, False)
                    + f'<td class="ffslot" style="text-align:center">{slot}</td>'
                    + cells(y, True) + '</tr>')

    def tot(side_rows):
        live_ = [z for z in side_rows if z]
        cur = (f'{sum((z["cur"] or 0) for z in live_):.1f}'
               if any(z["started"] for z in live_) else "—")
        return cur, f'{sum(z["proj"] for z in live_):.1f}'
    ca, pa = tot(A)
    cb, pb = tot(B)

    return (f'<div class="{scope}"><div class="ffwrap"><table class="fftable">'
            f'<tr><th class="{"good" if a_mine else ""}">{a_team.name}</th>'
            '<th class="ffnum">Cur</th><th class="ffnum">Proj</th><th></th>'
            '<th class="ffnuml">Cur</th><th class="ffnuml">Proj</th>'
            f'<th class="ffright {"good" if b_mine else ""}">{b_team.name}</th>'
            '</tr>'
            + "".join(rows)
            # 7 columns: name | cur | proj | slot | cur | proj | name
            + f'<tr><td></td><td class="ffnum"><b>{ca}</b></td>'
              f'<td class="ffnum"><b>{pa}</b></td>'
              f'<td class="ffslot" style="text-align:center">TOTAL</td>'
              f'<td class="ffnuml"><b>{cb}</b></td>'
              f'<td class="ffnuml"><b>{pb}</b></td><td></td></tr>'
            + '</table></div></div>')


def stm_alias(t: str) -> str:
    """Feed team code -> nflverse code, so LAR/JAC match a live game."""
    from ff.starts import TEAM_ALIASES
    return TEAM_ALIASES.get(t, t)


def _flags(rep, name, week: int | None = None) -> tuple[list[dict], int]:
    """Everything in one league that STILL wants a decision, worst first.

    Returns (items, locked) -- `locked` counts the suggestions dropped because
    the football has overtaken them. A lineup locks player by player as games
    kick off, so a swap is only real while BOTH men are still on the bench of a
    game that hasn't started; once Odunze's game is under way, "start Odunze" is
    not a task, it is a regret. Listing it anyway trains you to ignore the list.
    """
    out, locked = [], 0
    if not rep.get("drafted"):
        return out, locked
    L_, _r, _m = hydrate(name)
    KS = kickoff_states(week) if week else {}

    def started(team) -> bool:
        return bool(week) and ls_mod.has_played(team, KS)

    d = rep.get("diff") or {}
    for b in d.get("broken", []):
        # He is OUT and his game has kicked off: the slot is settled at whatever
        # it scored and there is nothing left to decide.
        if started(b.get("team")):
            locked += 1
            continue
        out.append({"sev": 0, "text": f"**{b.get('slot') or b['position']}: "
                    f"{b['name']}** is "
                    f"{(b.get('status') or 'unavailable').upper()}"
                    + (f" — {b['why']}" if b.get("why") else "")
                    + f" · that slot scores {b.get('week_points', 0):.1f}"})
    for sl in d.get("empty_slots", []):
        out.append({"sev": 0, "text": f"**{sl}** has nobody in it"})
    for sw in d.get("swaps", []):
        # EITHER side having started kills the move: you cannot pull a man whose
        # game is under way, and you cannot push one in either.
        if started(sw.get("out_team")) or started(sw.get("in_team")):
            locked += 1
            continue
        bad = sw["out_status"] in ("out", "unknown")
        # A swap worth +0.0 is not a job. Two players separated by less than a
        # tenth of a point are the same player as far as this model can tell,
        # and putting that on a to-do list is how a to-do list gets ignored.
        # Still shown on the Lineup page, where the column is a description of
        # the lineup rather than a demand -- and `close_calls` names them
        # explicitly under "Too close to call".
        if not bad and abs(sw.get("gain") or 0) < 0.1:
            continue
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
    return sorted(out, key=lambda x: x["sev"]), locked


# ---- Sunday ---------------------------------------------------------------
# Was two tabs, "Sunday" and "Watch", asking one question: what is on and who
# of mine is in it. Merged.
# ---- Lineup ---------------------------------------------------------------


def status_pill(stt: str) -> str:
    """Availability badge. Empty for a healthy player.

    "unknown" means `availability()` found no projection for the week -- not
    that anyone has been ruled out -- so it gets its own amber label. It used
    to render in the OUT colour, which said a player was ruled out on the
    strength of a missing number.
    """
    if not stt or stt == "ok":
        return ""
    label = {"unknown": "no proj"}.get(stt, stt)
    return (f'<span class="pill {"out" if stt == "out" else "risk"}">'
            f'{label}</span>')


def stake_rows(week: int) -> dict:
    """{NFL team: [enriched STARTER rows]} -- yours and your opponents'.

    The single source behind both Home's "playing right now" and the Sunday
    page, so the two cannot disagree about who is on the field or what he has.
    Starters only: a bench player's points are nobody's, not yours and not your
    opponent's, so he is noise on a page about what to root for.

    Guillotine leagues contribute no opponent rows -- there is no one rival
    there, and fifteen of them is not a thing anyone can watch.
    """
    hold = _holdings(week)
    PROG = game_progress(week)
    KST = kickoff_states(week)
    started = games_started(week)
    ALLOW = allowed_now(week)
    _sc = {L.name: L.scoring for L in leagues}
    lv = {L.name: (live_scores(L.name, week) if started else {})
          for L in leagues}
    out = {}
    for tm, v in hold.items():
        keep = []
        for h in v:
            if not h.get("starter"):
                continue
            row = (lv.get(h["league"]) or {}).get(h.get("team_id")) or {}
            sc = (row.get("starters") or {}).get(h.get("key"))
            has = ls_mod.has_played(h["nfl_team"], KST)
            sc = float(sc) if sc is not None else (0.0 if has else None)
            fr = ls_mod.remaining(h["nfl_team"], PROG)
            keep.append({**h, "cur": sc,
                         "live": special_mod.live_points(
                             _sc.get(h["league"]) or {}, h.get("position"),
                             sc or 0, h["proj"], fr,
                             ls_mod.pick(h["nfl_team"], ALLOW)),
                         "playing": ls_mod.is_playing(h["nfl_team"], KST)})
        if keep:
            out[tm] = keep
    return out


def stake_table(rows, scope="home") -> str:
    """One game's players: yours tinted green, your opponents' tinted red."""
    def _order(rs):
        # Grouped by PLAYER within each side, best group first. A man you roster
        # in three leagues is one thing to watch, not three scattered rows --
        # sorting purely by points split him up and put strangers between his
        # own lines.
        best = {}
        for h in rs:
            k = h["player"]
            best[k] = max(best.get(k, float("-inf")), h["live"])
        return sorted(rs, key=lambda h: (-best[h["player"]], h["player"],
                                         -h["live"]))
    ordered = (_order([h for h in rows if h.get("side") != "opp"])
               + _order([h for h in rows if h.get("side") == "opp"]))
    trs = []
    last_player = None
    for h in ordered:
        opp_row = h.get("side") == "opp"
        tint = "rgba(248,113,113,.08)" if opp_row else "rgba(74,222,128,.07)"
        cur = "—" if h["cur"] is None else f'{h["cur"]:.1f}'
        # Where he is heading against where he started. A point either way is
        # inside the noise and says nothing, so it stays neutral rather than
        # flashing at you.
        gap = h["live"] - (h["proj"] or 0)
        lcls = "warn" if abs(gap) <= 1.0 else ("good" if gap > 0 else "bad")
        # Repeat appearances of the same man are his other leagues; naming him
        # once keeps the group readable.
        same = h["player"] == last_player
        last_player = h["player"]
        nm = (f'<span class="dim">{h["player"]}</span>' if same
              else f'<b>{h["player"]}</b>')
        trs.append(
            f'<tr style="background:{tint}">'
            f'<td>{nm}{LIVEDOT if h["playing"] else ""}</td>'
            f'<td class="ffslot">{h["position"]}</td>'
            f'<td class="dim">{h["league"]}</td>'
            f'<td class="{"bad" if opp_row else "good"}">{h.get("who", "")}</td>'
            f'<td class="ffslot">{h["nfl_team"]}</td>'
            f'<td class="ffnum">{cur}</td>'
            f'<td class="ffnum {lcls}">{h["live"]:.1f}</td>'
            f'<td class="ffnum dim">{h["proj"]:.1f}</td></tr>')
    return (f'<div class="{scope}"><div class="ffwrap"><table class="fftable">'
            '<tr><th>Player</th><th>Pos</th><th>League</th><th>Who</th>'
            '<th>Tm</th><th class="ffnum">Current</th>'
            '<th class="ffnum">Proj now</th>'
            '<th class="ffnum">Pregame</th></tr>'
            + "".join(trs) + '</table></div></div>')


if nav == "Home":
    wk_all = _default_week()
    st.markdown(th.css(scope="home"), unsafe_allow_html=True)
    st.markdown(f"### Week {wk_all}")

    LIVE = games_started(wk_all)
    KST = kickoff_states(wk_all)
    PROGH = game_progress(wk_all)
    cards, todo, total_flags, locked_total = [], [], 0, 0
    for lg in leagues:
        try:
            r = report(lg.name, wk_all)
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
        f, _lk = _flags(r, lg.name, wk_all)
        total_flags += len(f)
        locked_total += _lk
        if f:
            todo.append((lg.name, f))
        # A dot means "on the field right now" -- for you, and for whoever you
        # are playing. It goes out at the final whistle rather than staying lit.
        _st = (r.get("_starter_teams") or {})
        _dot = lambda tid: (LIVEDOT if any(
            ls_mod.is_playing(x, KST) for x in (_st.get(tid) or [])) else "")
        cards.append((lg.name, head, sub, extra, opp,
                      (mine_p, them_p, margin, len(f), my_live, them_live,
                       _dot(r.get("_my_id")), _dot(r.get("_opp_id")))))

    # --- odds, one card per league ---
    html = ['<div class="home"><div class="ffwrap">',
            '<h2>Where you stand</h2>',
            f'<div class="ffsub">week {wk_all} · five leagues</div>',
            '<table class="fftable"><tr><th>League</th><th>This week</th>'
            '<th></th>'
            + ('<th class="ffnum">Current</th><th class="ffnum">Opp now</th>'
               if LIVE else '')
            + ('<th class="ffnum">Proj now</th>'
               '<th class="ffnum">Them now</th>' if LIVE else
               '<th class="ffnum">Pregame</th><th class="ffnum">Them</th>')
            + '<th class="ffnum">Margin</th><th>Season</th>'
              '<th class="ffnum">To do</th></tr>']
    for nm, head, sub, extra, opp, nums in cards:
        if nums is None:
            html.append(f'<tr><td><b>{nm}</b></td><td class="dim" '
                        f'colspan="{9 if LIVE else 7}">{sub}</td></tr>')
            continue
        mine_p, them_p, margin, nf, my_live, them_live, my_dot, opp_dot = nums
        try:
            pct = float(str(head).rstrip("%"))
        except ValueError:
            pct = 50.0
        cls = "good" if pct >= 55 else ("bad" if pct < 45 else "warn")
        mcls = "good" if (margin or 0) > 0 else ("bad" if (margin or 0) < 0 else "dim")
        todo_cell = (f'<span class="bad">{nf}</span>' if nf
                     else '<span class="good">clear</span>')
        html.append(
            f'<tr><td><b>{nm}</b>{my_dot}</td>'
            f'<td class="{cls}" style="font-size:19px;font-weight:700">{head}</td>'
            f'<td class="dim" style="font-size:12px">{sub}<br>{opp}{opp_dot}</td>'
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

    # One expander per league: your lineup beside your opponent's, slot by slot.
    # Same renderer the Matchups tab uses, so the two can never disagree.
    for lg in leagues:
        try:
            r = report(lg.name, wk_all)
        except Exception:
            continue
        if not r.get("drafted"):
            continue
        L_, _rw, _mt = hydrate(lg.name)
        try:
            tms_ = rosters_mod.all_teams(L_, wk_all, get_blob())
            me_t = next((t for t in tms_ if t.mine), None)
            opp_t = rosters_mod.opponent_of(L_, wk_all, tms_)
            if opp_t is None and L_.raw.get("guillotine"):
                # No opponent -- the field is the opponent, so line yourself up
                # against whoever is currently last.
                wpx = lineup_mod.weekly_points(wk_all, L_.scoring)
                lowest, lo = None, None
                for t in rosters_mod.active_teams(tms_):
                    mu, _s, _d = winprob_mod.live_project_team(
                        t, wpx, L_, live_scores(lg.name, wk_all).get(t.team_id),
                        PROGH, week=wk_all, allowed=allowed_now(wk_all))
                    if lo is None or mu < lo:
                        lowest, lo = t, mu
                opp_t = lowest if lowest and not lowest.mine else None
            if not (me_t and opp_t):
                continue
            wpx = lineup_mod.weekly_points(wk_all, L_.scoring)
            with st.expander(f"{lg.name}  ·  you vs {opp_t.name}"):
                bk_ = {key(rr["name"], rr["position"]): rr for rr in _rw}
                st.markdown(
                    side_by_side(L_, me_t, opp_t, wk_all,
                                 live_scores(lg.name, wk_all), PROGH, KST,
                                 bk_, get_blob(), scope="home"),
                    unsafe_allow_html=True)
        except Exception as e:
            st.caption(f"{lg.name}: couldn't build matchup — "
                       f"{type(e).__name__}: {e}")

    # --- playing right now ---------------------------------------------
    # During games this is the thing you actually want: who is on the field
    # this minute, what they have, and whether that is ahead of plan. Grouped
    # by GAME because that is how the afternoon is organised -- one game can
    # matter to you in three leagues at once.
    #
    # Same rows and same table as the Sunday page (stake_rows / stake_table):
    # yours and your opponents', starters only.
    _live_games = [g for g in (_games(wk_all) or []) if g.get("state") == "in"]
    if _live_games:
        st.markdown("")
        st.markdown("#### Playing right now")
        _stake = stake_rows(wk_all)
        _shown = 0
        for g in _live_games:
            rws_ = _stake.get(g["home"], []) + _stake.get(g["away"], [])
            if not rws_:
                continue
            _shown += 1
            mine_n = sum(1 for h in rws_ if h.get("side") != "opp")
            st.markdown(
                f'<div class="home"><h2>{g["away"]} {g.get("away_score", 0)} — '
                f'{g.get("home_score", 0)} {g["home"]}</h2>'
                f'<div class="ffsub">{g.get("detail") or ""} · '
                f'{g.get("network") or ""} · '
                f'<span class="good">{mine_n} for</span> · '
                f'<span class="bad">{len(rws_) - mine_n} against</span></div>'
                f'</div>', unsafe_allow_html=True)
            st.markdown(stake_table(rws_, "home"), unsafe_allow_html=True)
        if not _shown:
            st.caption("None of your starters are in a game that's live.")
        else:
            st.caption("**Green rows are yours, red rows are against you.** A "
                       "green dot means he is on the field right now. "
                       "**Current** is banked; **Proj now** is where he "
                       "finishes at his projected rate for the football left — green "
                       "if that is ahead of his pregame number, red if "
                       "behind, amber inside a point either way.")

    # --- what needs you ---
    st.markdown("")
    st.markdown('<div class="home">', unsafe_allow_html=True)
    st.markdown(f"#### Needs you  ·  {total_flags}" if total_flags
                else "#### Needs you")
    st.caption(URGENT_HELP)
    if not todo:
        st.markdown('<div class="home"><div class="ffok">Nothing needs a '
                    'decision. Every lineup is legal, everyone starting is '
                    'playing, and none of them can be improved.</div></div>'
                    if not locked_total else
                    '<div class="home"><div class="ffok">Nothing you can still '
                    'act on. Every remaining problem is in a game that has '
                    'already kicked off.</div></div>',
                    unsafe_allow_html=True)
    for nm, items in todo:
        st.markdown(f"**{nm}**")
        for it in items:
            (st.error if it["sev"] == 0 else st.warning)(it["text"])
    # Said out loud rather than silently dropped: the difference between "your
    # lineup is fine" and "it is too late to fix" matters, and only one of them
    # is worth remembering for next week.
    if locked_total:
        st.caption(f"{locked_total} suggestion"
                   f"{'s' if locked_total != 1 else ''} not shown — "
                   "those games have started, so the slots are locked.")
    st.markdown('</div>', unsafe_allow_html=True)
    st.stop()

if nav == "Sunday":
    from ff import live as live_mod
    st.header("Sunday")
    c1, c2 = st.columns([1, 4])
    wk = int(c1.number_input("Week", 1, 18, _default_week(), key="sun_wk"))
    if c2.button("Refresh now", key="sun_refresh"):
        _games.clear(); _holdings.clear()
    st.caption("Your players are pulled from every league that has drafted, so "
               "one game can matter to you several times over. This page does "
               "NOT update on its own — reload, or hit Refresh now, to re-read "
               "the scoreboard.")

    try:
        gs = _games(wk)
        hold = _holdings(wk)
    except Exception as e:
        st.error(f"couldn't load live data: {type(e).__name__}: {e}")
        gs, hold = [], {}

    if not hold:
        st.info("No drafted leagues yet — nothing to follow.")

    # "What to put on" is about YOUR afternoon, so it sees only your side.
    mine_only = {t: [h for h in v if h.get("side") != "opp" and h.get("starter")]
                 for t, v in hold.items()}
    mine_only = {t: v for t, v in mine_only.items() if v}
    try:
        slots = live_mod.watch_by_slot(gs, mine_only)
    except Exception:
        slots = []
    # One line per window, not a table each. The full team-by-team detail is in
    # the game list below, and printing it twice in two different shapes was
    # the reason this page read as two designs arguing with each other.
    if slots:
        st.caption("**Best window:** " + " · ".join(
            f"{s_['slot']} → {s_['pick']['away']} @ {s_['pick']['home']} "
            f"({s_['pick']['network']})" for s_ in slots))

    st.markdown(th.css(scope="sun"), unsafe_allow_html=True)

    # Same source and same table as Home's "playing right now".
    starters_by_team = stake_rows(wk)

    def _rows_for(g):
        return (starters_by_team.get(g["home"], [])
                + starters_by_team.get(g["away"], []))

    # Only games you actually have a stake in, either way. A game with none of
    # your starters and none of your opponents' is not a game you have any
    # reason to watch.
    slate = [g for g in gs if _rows_for(g)]
    st.subheader(f"Games that matter ({len(slate)})")
    st.caption("Only games holding a starter of yours or of the team playing "
               "you. **Green rows are yours, red rows are against you.** A "
               "green dot means he is on the field right now. **Current** is "
               "banked, **Proj now** is where he finishes at his projected rate "
               "for the football left — green if that is ahead of his "
               "pregame number, red if behind, amber inside a point "
               "either way.")

    # Time first, then how much of the game is yours.
    for g in sorted(slate, key=lambda x: ((x.get("kickoff") or ""),
                                          -len(_rows_for(x)))):
        here = _rows_for(g)
        head_ = (f"{g['away']} {g['away_score']} — {g['home_score']} {g['home']}"
                 if g["state"] != "pre" else f"{g['away']} @ {g['home']}")
        mine_n = sum(1 for h in here if h.get("side") != "opp")
        dot = LIVEDOT if g["state"] == "in" else ""
        st.markdown(
            f"**{head_}**{dot}  ·  {g['detail']}  ·  {g['network']}  ·  "
            f"<span class='good'>{mine_n} for</span> · "
            f"<span class='bad'>{len(here) - mine_n} against</span>",
            unsafe_allow_html=True)
        st.markdown(stake_table(here, "sun"), unsafe_allow_html=True)
        st.divider()
    st.stop()


# Keepers only exists where the league actually has them, so four of five
# leagues stop carrying a tab that could only ever say "not configured".
HAS_KEEPERS = bool((cfg.get("keepers") or {}).get("max"))
# Lineup leads: on any given day the question is "is my lineup right", not
# "summarise my week".
# Same principle as Keepers: a house rule only one league plays, so it is read
# from that league's config rather than hardcoded by name. Set `donuts: true`
# on any other league that adopts it.
HAS_DONUTS = bool(cfg.get("donuts"))
_names = ["Lineup", "Report", "Matchups", "Waivers", "Trades"]
if HAS_KEEPERS:
    _names.append("Keepers")
if HAS_DONUTS:
    _names.append("Donuts")
_T = dict(zip(_names, st.tabs(_names)))


t_report, t_lineup, t_match = _T.get("Report"), _T.get("Lineup"), _T.get("Matchups")
t_waiver, t_trade, t_keep = _T.get("Waivers"), _T.get("Trades"), _T.get("Keepers")
t_donut = _T.get("Donuts")


# ---------------------------------------------------------------------------
# Sunday + Watch are cross-league on purpose: your Sunday isn't organised by
# league, it's organised by which games are on.
# ---------------------------------------------------------------------------
with t_report:
    week_now = _default_week()
    st.markdown(th.css(scope="lg"), unsafe_allow_html=True)
    rep = report(L.name, week_now)

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
            pill = status_pill(stt)
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
        filled, opt_bench = lineup_mod.optimize(mine, L)
        diff = lineup_mod.actual_vs_optimal(mine, filled,
                                            (_me.starters if _me else []))
        have_actual = bool(diff["actual"])
        shown = (diff["actual"] if have_actual
                 else [{**q, "slot": sl} for sl, v in filled.items() for q in v])

        # Bench is the complement of the STARTERS TABLE, not the optimizer's
        # leftovers. `optimize` benches whoever the maths wants benched, so on
        # any week with a suggested swap its bench held the man you are actually
        # starting -- he appeared in both tables -- while the suggested
        # replacement, sitting in `filled`, appeared in neither. The two tables
        # have to partition the same roster.
        _starting = {key(q["name"], q.get("position")) for q in shown}
        bench = [q for q in mine
                 if key(q["name"], q.get("position")) not in _starting]
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
            # Projected FINAL: what is banked plus what is still to come. This
            # is the number that actually moves during the day; "pregame" is
            # where the week started and never accounts for points scored.
            live_final = 0.0
            _allw = allowed_now(wk)
            for q in shown:
                fr = ls_mod.remaining(q.get("team"), PROG)
                sc_ = (got or {}).get(key(q["name"], q.get("position")))
                sc_ = 0.0 if sc_ is None else float(sc_)
                # Same rule as every per-player cell below, so the header total
                # is the column's sum rather than a second opinion about it.
                live_final += special_mod.live_points(
                    L.scoring, q.get("position"), sc_,
                    q.get("week_points") or 0, fr,
                    ls_mod.pick(q.get("team"), _allw))
            # Only swaps between players who have BOTH not kicked off are
            # actionable -- a lineup locks player by player as games start, so
            # "leave on bench" against someone who already played is noise.
            fixable = sum(
                sw["gain"] for sw in diff["swaps"]
                if not ls_mod.has_played(
                    next((q.get("team") for q in shown if q["name"] == sw["out"]),
                         None), KSTATE))
            m[0].metric("Current",
                        f"{my_lv.get('total', 0):.1f}" if _any_mine else "—",
                        "scored so far" if _any_mine else "nobody has kicked off")
            m[1].metric("Projected final", f"{live_final:.1f}",
                        f"{live_final - cur:+.1f} vs pregame")
            m[2].metric("Pregame projection", f"{cur:.1f}")
            m[3].metric("Still fixable", f"{fixable:+.1f}",
                        "swaps you can still make", delta_color="inverse")
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
            if sw and ls_mod.has_played(a_.get("team"), KSTATE):
                sw = None          # his game has started; the slot is locked
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
                    lp = special_mod.live_points(
                        L.scoring, a_.get("position"), pts_now, proj_now, fr,
                        ls_mod.pick(a_.get("team"), allowed_now(wk)))
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
                f'<td class="{cls}"><b>{nm_}</b>{dot_if_live(a_.get("team"), KSTATE)}</td>'
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

        st.subheader(f"Bench ({len(bench)})")
        # Same columns as the starters table. A bench player's points are not
        # yours, but you still need to know what he did -- that is the whole
        # basis for next week's decision, and for whether a swap you skipped
        # cost you anything.
        allpts = (my_lv.get("players") or {}) if LIVE else {}

        def _adds(b):
            """Points he would add if swapped into your CURRENT lineup.

            Compared against the weakest starter in a slot he is eligible for,
            skipping any whose game has begun -- that slot is locked and the
            swap is not available however much better he looks.
            """
            best = 0.0
            for stp in shown:
                sl = stp.get("slot") or stp.get("position")
                elig = draft_mod.SLOT_ELIGIBILITY.get(sl, {sl})
                if b.get("position") not in elig:
                    continue
                if LIVE and ls_mod.has_played(stp.get("team"), KSTATE):
                    continue
                best = max(best, (b.get("week_points") or 0)
                           - (stp.get("week_points") or 0))
            return best

        brows = []
        for q in sorted(bench, key=lambda x: -(x.get("week_points") or 0)):
            stt = q.get("status") or "ok"
            pill = status_pill(stt)
            cells = ""
            if LIVE:
                if not ls_mod.has_played(q.get("team"), KSTATE):
                    cells = ('<td class="ffnum dim">—</td>'
                             '<td class="ffnum dim">—</td>')
                else:
                    sc_ = allpts.get(key(q["name"], q.get("position")))
                    sc_ = 0.0 if sc_ is None else float(sc_)
                    pj = q.get("week_points") or 0
                    fr = ls_mod.remaining(q.get("team"), PROG)
                    lpj = sc_ + pj * fr
                    pace = pj * (1 - fr)
                    c1 = ("good" if sc_ >= pace
                          else "warn" if sc_ >= pace * .6 else "bad")
                    c2 = ("good" if lpj >= pj
                          else "warn" if lpj >= pj * .8 else "bad")
                    cells = (f'<td class="ffnum {c1}">{sc_:.1f}</td>'
                             f'<td class="ffnum {c2}">{lpj:.1f}</td>')
            add = _adds(q)
            add_cell = (f'<td class="ffnum good">+{add:.1f}</td>'
                        if add > 0.05 else '<td class="ffnum dim">—</td>')
            brows.append(
                f'<tr><td class="ffslot">'
                f'{q.get("pos_rank", q["position"])}</td>'
                    f'<td><b>{q["name"]}</b>{dot_if_live(q.get("team"), KSTATE)} {pill}</td>'
                f'<td class="dim">{q.get("team") or ""}</td>'
                f'<td class="dim">{q.get("opponent", "")}</td>'
                + cells
                + f'<td class="ffnum dim">'
                  f'{q.get("week_points", 0):.1f}</td>'
                + add_cell + '</tr>')
        st.markdown(
            '<div class="lp"><div class="ffwrap"><table class="fftable">'
            '<tr><th>Pos</th><th>Player</th><th>Tm</th><th>Opp</th>'
            + ('<th class="ffnum">Scored</th><th class="ffnum">Proj now</th>'
               if LIVE else '')
            + '<th class="ffnum">Pregame</th>'
              '<th class="ffnum">Adds to lineup</th></tr>'
            + "".join(brows) + '</table></div></div>',
            unsafe_allow_html=True)
        st.caption("**Adds to lineup** is what he would gain you if swapped in "
                   "now, against the weakest starter he is eligible to replace. "
                   "A dash means he improves nothing — and starters whose games "
                   "have begun are excluded, since those slots are locked.")

        calls = lineup_mod.close_calls(filled, opt_bench, L)
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
    if rosters_mod.has_drafted(L, tms):
        # Eliminated managers keep an empty row; they are not the field.
        tms = rosters_mod.active_teams(tms)
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
                    t, wpm, L, mlv.get(t.team_id), PROGM, week=wkm,
                    allowed=allowed_now(wkm))
            else:
                mu, sd, _st = winprob_mod.project_team(t, wpm, L, week=wkm)
            proj[t.team_id] = (t, mu, sd)
        _plab = "Proj now" if _lv_any else "Pregame"
        pairs = rosters_mod.all_matchups(L, wkm, tms)
        MLIVE = games_started(wkm)
        MST = kickoff_states(wkm)

        if not pairs:
            # Guillotine: no matchups, so the league IS the scoreboard and the
            # only thing that matters is distance from the bottom.
            #
            # ASCENDING. In a guillotine league only the bottom matters, so the
            # bottom is where the eye should land: row 1 is the team currently
            # going home. Sorted FIRST -- the head-to-head expanders below read
            # from `order`, and building them before it existed raised
            # NameError the moment this league had no pairs.
            order = sorted(proj.values(), key=lambda x: x[1])
            low = order[0][1]
            second = order[1][1] if len(order) > 1 else low
            rws = []
            for i, (t, mu, sd) in enumerate(order, 1):
                me_ = t.mine
                cls = "good" if me_ else ""
                # One meaning only: how far clear of the elimination line you
                # are. Last place has no cushion, so it is 0.0 -- showing the
                # distance to the next team up instead put two different
                # quantities in one column. That distance is not lost: it is
                # simply row 2's cushion.
                marg = mu - low
                mcls = ("bad" if marg < 8 else "warn" if marg < 20 else "good")
                if i == 1:
                    mcls = "bad"          # on the line
                got_ = (mlv.get(t.team_id) or {}).get("total")
                # STARTERS, not the whole roster. A bench player whose game has
                # kicked off does not make the team's score meaningful -- only
                # starters score, so checking `players` made a team with an
                # already-played bench piece show 0.0 instead of a dash.
                played = MLIVE and any(
                    ls_mod.has_played(q.get("team"), MST)
                    for q in (t.starters or []))
                lcell = ""
                if MLIVE:
                    lcell = (f'<td class="ffnum">{got_:.1f}</td>'
                             if played and got_ is not None
                             else '<td class="ffnum dim">—</td>')
                rws.append(
                    f'<tr style="{"background:rgba(74,222,128,.06)" if me_ else ""}">'
                    f'<td class="{"bad" if i == 1 else "dim"}">{i}</td>'
                    f'<td class="{cls}"><b>{t.name}</b>{team_dot(t, MST)}'
                    f'{" ← you" if me_ else ""}</td>'
                    + lcell
                    + f'<td class="ffnum">{mu:.1f}</td>'
                      f'<td class="ffnum dim">±{sd:.0f}</td>'
                      f'<td class="ffnum {mcls}">{marg:+.1f}</td></tr>')
            st.markdown('<div class="mu"><div class="ffwrap"><table class="fftable">'
                        '<tr><th>#</th><th>Team</th>'
                        + ('<th class="ffnum">Current</th>' if MLIVE else '')
                        + '<th class="ffnum">Projected</th>'
                          '<th class="ffnum">±</th>'
                          '<th class="ffnum">Cushion</th></tr>'
                        + "".join(rws) + '</table></div></div>',
                        unsafe_allow_html=True)

            _me_t = next((t for t, _m, _s in proj.values() if t.mine), None)
            if _me_t:
                for t, mu, _sd in order:
                    if t.team_id == _me_t.team_id:
                        continue
                    with st.expander(f"you  vs  {t.name}"):
                        st.markdown(
                            side_by_side(L, _me_t, t, wkm, mlv, PROGM, MST,
                                         by_key, blob, scope="mu"),
                            unsafe_allow_html=True)
            st.caption("Guillotine — no matchups, so the whole field is the "
                       "table. **Sorted worst first**: row 1 is the team "
                       "currently going home. **Cushion** is how far clear of "
                       "the elimination line you are — zero if you are on it. "
                       "Row 2's cushion is therefore what row 1 has to make up.")
        else:
            rws = []
            for h, a_ in pairs:
                if h not in proj or a_ not in proj:
                    continue
                th_, hm, hs = proj[h]
                ta_, am, asd = proj[a_]
                pw = winprob_mod.head_to_head((hm, hs), (am, asd))
                def _live_total(t):
                    # Starters only -- see the note in the guillotine table.
                    if not any(ls_mod.has_played(q.get("team"), MST)
                               for q in (t.starters or [])):
                        return None
                    return (mlv.get(t.team_id) or {}).get("total")
                hl, al = _live_total(th_), _live_total(ta_)
                mine_here = th_.mine or ta_.mine
                hi = "background:rgba(74,222,128,.06)" if mine_here else ""
                hcls = "good" if pw >= .5 else "bad"
                acls = "good" if pw < .5 else "bad"
                rws.append(
                    f'<tr style="{hi}">'
                    f'<td class="{"good" if th_.mine else ""}">'
                    f'<b>{th_.name}</b>{team_dot(th_, MST)}'
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
                    + f'<td class="{"good" if ta_.mine else ""}">'
                      f'<b>{ta_.name}</b>{team_dot(ta_, MST)}'
                    f'{" ← you" if ta_.mine else ""}</td></tr>')
            st.markdown('<div class="mu"><div class="ffwrap"><table class="fftable">'
                        + ('<tr><th>Home</th>'
                           + ('<th class="ffnum">Current</th>' if MLIVE else '')
                           # The value in this column is live_project_team once
                           # anything has kicked off, so the header has to say so
                           # -- it read "Pregame" while showing a decaying number.
                           + f'<th class="ffnum">{_plab}</th>'
                             '<th class="ffnum">Win</th><th></th>'
                             f'<th class="ffnum">Win</th><th class="ffnum">{_plab}</th>'
                           + ('<th class="ffnum">Current</th>' if MLIVE else '')
                           + '<th>Away</th></tr>')
                        + "".join(rws) + '</table></div></div>',
                        unsafe_allow_html=True)
            for h, a_ in pairs:
                if h not in proj or a_ not in proj:
                    continue
                th_, hm, _hs = proj[h]
                ta_, am, _as = proj[a_]
                mark = " ←" if (th_.mine or ta_.mine) else ""
                with st.expander(f"{th_.name}  vs  {ta_.name}{mark}"):
                    st.markdown(
                        side_by_side(L, th_, ta_, wkm, mlv, PROGM, MST,
                                     by_key, blob, scope="mu"),
                        unsafe_allow_html=True)
            st.caption(
                "Every game this week, scored under this league's rules. Odds "
                "come from the same distribution as your own matchup, so they "
                "are directly comparable. " + (
                    "**Proj now** is points banked plus what is still projected "
                    "from the football left, so it moves all day and is directly "
                    "comparable to the totals on the lineup and home pages."
                    if _lv_any else
                    "**Pregame** is the projection published before kickoff; it "
                    "starts decaying into a live number once games begin."))

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
    # Was hardcoded to "No FAAB in any of your leagues", which stopped being
    # true the moment work was wired up -- and it sat directly above a panel
    # reading "FAAB LEFT $1000". Read the league's own style instead.
    st.caption(f"**{L.waiver_note}.** " + (
        "Claims cost money, not waiver position: bid on as many players as you "
        "like and each is settled on its own, so the call is how much, not "
        "whether."
        if L.waiver_style == "faab" else
        "No FAAB here — the cost of a claim is your waiver position, so the "
        "call is claim, wait, or skip."))

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

            # Candidates come from the BOARD -- every free agent -- and usage is
            # joined on where it exists. This used to iterate the usage frame
            # instead, which meant a player with no history could not appear
            # however much he would add: a rookie is absent from last season's
            # data entirely, and the tab falls back to last season whenever this
            # one has no games through week-1 yet. Jadarian Price would have
            # added 32.4 to the starting lineup and was invisible here while
            # sitting near the top of Best available two sections below.
            #
            # Scoring all 385 free agents costs 0.01s, so the filter was never
            # buying speed either.
            tmap = {r.k: r for r in trend.itertuples()}
            cands = []
            for row in rows:
                k_ = key(row["name"], row["position"])
                if k_ in taken or row.get("pos_rank_n") is None:
                    continue
                tr_ = tmap.get(k_)
                gain = draft_mod.lineup_value(mine + [row], L, L.replacement) - base
                # The snap gate stays, but only as a NOISE filter on players who
                # add nothing. It must never hide someone who improves the
                # lineup, which is the one question this section answers.
                if gain <= 0 and (tr_ is None or tr_.snap_pct_recent <= 0.25):
                    continue
                call, why = weekly_mod.claim_call(gain, heat.get(k_),
                                                  L.waiver_style, not replay)
                cands.append({"Player": row["name"], "Pos": row["pos_rank"],
                              "Adds": round(gain),
                              "PPG": round(tr_.ppg, 1) if tr_ is not None else 0.0,
                              "Snap%": (f"{tr_.snap_pct_recent*100:.0f}%"
                                        if tr_ is not None else "—"),
                              "ΔSnap": (f"{tr_.snap_delta*100:+.0f}"
                                        if tr_ is not None else "—"),
                              "Tgt": (round(tr_.targets_recent, 1)
                                      if tr_ is not None else 0.0),
                              "ΔTgt": (f"{tr_.tgt_delta:+.1f}"
                                       if tr_ is not None else "—"),
                              "Call": call, "Why": why, "_g": gain,
                              "_ds": (tr_.snap_delta if tr_ is not None else None)})

        useful = sorted([c for c in cands if c["_g"] > 0], key=lambda c: -c["_g"])
        st.subheader("Improves your starting lineup")
        if not useful:
            st.info("**Nothing on the wire cracks your starting lineup** — with a "
                    "full healthy roster that is the normal answer, since a "
                    "player who doesn't start adds zero by definition. The best "
                    "available are listed below anyway: they are who you'd take "
                    "if an injury opened a slot.")
        else:
            st.dataframe(
                pd.DataFrame(useful).drop(columns=["_g", "_ds"], errors="ignore"),
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
        _top = _free[:20]
        _adds = {r["name"]: draft_mod.lineup_value(mine + [r], L, L.replacement)
                 - _base for r in _top}

        # What he is worth in a guillotine league, priced in weeks of survival.
        # Only for a player who actually improves the lineup: the model gives
        # everyone else zero extra weeks by construction, so pricing them is
        # both pointless and the honest answer.
        _bids = {}
        _is_gl = bool(L.raw.get("guillotine")) and L.waiver_style == "faab"
        if _is_gl and any(v > 0 for v in _adds.values()):
            try:
                from bakeoff import weekly_score as _wsc0
                from ff import faab as _faab0
                _bud0 = faab_balance(L.name) or 0
                _wl0 = max(1, regular_weeks(L.name) - week + 1)
                for r in _top:
                    if _adds[r["name"]] <= 0:
                        continue
                    _bids[r["name"]] = _faab0.guillotine_value(
                        rosters_mod.active_teams(all_t), by_key, L,
                        byes_all(), _wsc0, r,
                        _bud0, _wl0, sims=1500)["max"]
            except Exception as _e:
                st.caption(f"couldn't price the wire: {type(_e).__name__}")

        _cols = [{
            "Player": r["name"], "Pos": r["pos_rank"],
            "Team": r.get("team") or "",
            "Season": round(r["points"]),
            "VORP": round(r["vorp"]),
            "Adds now": round(_adds[r["name"]], 1),
            **({"Max bid": f"${_bids.get(r['name'], 0)}"} if _is_gl else {}),
        } for r in _top]
        st.dataframe(pd.DataFrame(_cols), hide_index=True, width="stretch")
        st.caption("**Adds now** is what he'd add to THIS week's starting "
                   "lineup; **Season** and **VORP** are why he's worth a roster "
                   "spot even when that is zero."
                   + ("  **Max bid** is the most he is worth to you: your budget "
                      "scaled by the extra weeks alive he buys, plus a premium "
                      "for how many rivals need the same slot. $0 means he adds "
                      "nothing to your starting lineup, which buys zero weeks — "
                      "that is a real answer, not a missing one." if _is_gl
                      else ""))

        st.subheader("Usage risers — opportunity moves before production")
        # Only players with real usage data -- a "riser" needs a baseline to
        # have risen from.
        movers = sorted([c for c in cands if c.get("_ds") is not None],
                        key=lambda c: -c["_ds"])[:12]
        st.dataframe(pd.DataFrame(movers)[
            ["Player", "Pos", "Snap%", "ΔSnap", "Tgt", "ΔTgt", "PPG"]],
            hide_index=True, width='stretch')

        if L.raw.get("guillotine") and L.waiver_style == "faab":
            st.divider()
            st.subheader("What to bid")
            bud = faab_balance(L.name) or 0
            wl = max(1, regular_weeks(L.name) - week + 1)
            rep_ = report(L.name, week)
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
                        rosters_mod.active_teams(all_t), by_key, L, byes_all(),
                        _wsc, row, bud, wl, sims=1500)
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


# ---------------------------------------------------------------------------
# Donuts. A house rule in 719: a STARTER who finishes his game on 0.0 or fewer
# points pays a penalty. Enabled per league by `donuts: true` in leagues.yaml.
if t_donut is not None:
    with t_donut:
        st.markdown(th.css(scope="dn"), unsafe_allow_html=True)
        st.subheader("Donut tracker")
        wkd = int(st.slider("Week", 1, 18, _default_week(), key="dn_wk"))
        try:
            tms_d = rosters_mod.all_teams(L, wkd, blob)
            lv_d = live_scores(L.name, wkd)
            KS_d = kickoff_states(wkd)
            PER_d = ls_mod.game_period(_games(wkd))
        except Exception as e:
            st.error(f"couldn't read the league: {type(e).__name__}: {e}")
            tms_d, lv_d, KS_d, PER_d = [], {}, {}, {}

        # STARTERS only. A bench player scores nothing for anyone by definition,
        # so counting him would hand every team a dozen donuts a week and make
        # the list meaningless. If the house rule really does cover the whole
        # roster, this is the line to change.
        served, watch = [], []
        for t in tms_d:
            row = (lv_d.get(t.team_id) or {})
            got = row.get("starters") or {}
            for q in (t.starters or []):
                if not q.get("name"):
                    continue
                pos = (q.get("position") or "").upper()
                pos = "DST" if pos in ("DEF", "D/ST") else pos
                pts = got.get(key(q["name"], pos))
                if pts is None:
                    continue
                pts = float(pts)
                if pts > 0:
                    continue
                st_, _per = ls_mod.pick(q.get("team"), PER_d, ("pre", 0))
                rec = {"team": t.name, "mine": bool(t.mine), "name": q["name"],
                       "pos": pos, "slot": q.get("slot"), "nfl": q.get("team"),
                       "pts": pts}
                if st_ == "post":
                    served.append(rec)
                elif ls_mod.second_half(q.get("team"), PER_d):
                    watch.append(rec)

        def _dn_table(rows, empty):
            if not rows:
                st.markdown(f'<div class="dn"><div class="ffok">{empty}</div>'
                            '</div>', unsafe_allow_html=True)
                return
            trs, last = [], None
            for r in sorted(rows, key=lambda x: (not x["mine"], x["team"],
                                                 x["pts"], x["name"])):
                cell = "" if r["team"] == last else (
                    f'<b class="{"good" if r["mine"] else ""}">{r["team"]}</b>'
                    + (" ← you" if r["mine"] else ""))
                last = r["team"]
                trs.append(
                    f'<tr style="background:rgba(248,113,113,.07)">'
                    f'<td>{cell}</td>'
                    f'<td><b>{r["name"]}</b>'
                    f'{dot_if_live(r["nfl"], KS_d)}</td>'
                    f'<td class="ffslot">{r["slot"] or r["pos"]}</td>'
                    f'<td class="ffslot">{r["nfl"]}</td>'
                    f'<td class="ffnum bad">{r["pts"]:.1f}</td></tr>')
            st.markdown(
                '<div class="dn"><div class="ffwrap"><table class="fftable">'
                '<tr><th>Team</th><th>Player</th><th>Slot</th><th>Tm</th>'
                '<th class="ffnum">Points</th></tr>'
                + "".join(trs) + '</table></div></div>', unsafe_allow_html=True)

        st.markdown(f"#### Served  ·  {len(served)}")
        _dn_table(served, "No donuts yet. Every starter whose game has "
                          "finished is on the board.")
        st.caption("Starters whose game is **final** on 0.0 or fewer. These are "
                   "settled — nothing can move them.")

        st.markdown("")
        st.markdown(f"#### On watch  ·  {len(watch)}")
        _dn_table(watch, "Nobody in the second half is sitting on a donut.")
        st.caption("Starters **past halftime** still on 0.0 or fewer. Not "
                   "settled: a catch in the 4th wipes it. Players whose game "
                   "has not reached the 3rd quarter are excluded — it is too "
                   "early to mean anything.")
