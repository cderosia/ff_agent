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

st.set_page_config(page_title="FF Agent", page_icon="🏈", layout="wide")

CSS = """
<style>
  .block-container{padding-top:2.2rem;max-width:1200px}
  [data-testid="stMetricValue"]{font-size:1.5rem}
  .pill{display:inline-block;padding:2px 9px;border-radius:4px;font-size:11px;
        font-weight:700;letter-spacing:.06em;text-transform:uppercase}
  .claim{background:rgba(11,110,91,.15);color:#0B6E5B}
  .wait{background:rgba(138,93,24,.15);color:#8A5D18}
  .skip{background:rgba(120,120,130,.15);color:#6b7280}
  .muted{color:#6b7280;font-size:13px}
  h1{font-size:1.7rem !important}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


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


@st.cache_resource(show_spinner="loading player index…")
def get_blob():
    p = ROOT / "data" / "raw" / "sleeper_players.json"
    if p.exists():
        return json.loads(p.read_text())
    import requests
    d = requests.get("https://api.sleeper.app/v1/players/nfl", timeout=120).json()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d))
    return d


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

names = [l.name for l in leagues]
choice = st.sidebar.radio("League", names, index=0, key="league")
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
    st.cache_data.clear()
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
(t_dash, t_report, t_sunday, t_lineup, t_waiver, t_trade, t_keep) = st.tabs(
    ["All leagues", "Report", "Sunday", "Lineup", "Waivers", "Trades",
     "Keepers"])


# ---------------------------------------------------------------------------
# Sunday + Watch are cross-league on purpose: your Sunday isn't organised by
# league, it's organised by which games are on.
# ---------------------------------------------------------------------------
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


# ---- Report ---------------------------------------------------------------
# One page, no controls. The whole point is that Sunday morning you open this
# and it already says what to do -- picking a week and a report type and then
# pressing Build was three decisions before any answer appeared.
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
    calls = lineup_mod.close_calls(filled, bench, L_)
    total = sum(p["week_points"] for ps in filled.values() for p in ps)

    # Odds. winprob carries a DISTRIBUTION rather than a point estimate, which
    # is the only way to answer "am I likely to win" -- two lineups at 118 and
    # 112 are not a 6-point favourite in any useful sense.
    try:
        opp = rosters_mod.opponent_of(L_, week, teams)
        odds = winprob_mod.league_odds(teams, wp, L_, opponent=opp, week=week)
    except Exception as e:
        odds = {"error": f"{type(e).__name__}: {e}"}

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
            "season": season, "through": through, "live": live,
            "unavailable": [p for p in pool if p["status"] in ("out", "unknown")]}


with t_report:
    week_now = _default_week()
    st.header(f"{L.name} — week {week_now}")
    rep = build_report(L.name, week_now)

    if not rep.get("drafted"):
        st.info("No roster yet — this fills in the moment the draft finishes.")
    else:
        o = rep.get("odds") or {}
        mode = o.get("mode")
        m = st.columns(4)
        m[0].metric("Projected", f"{rep['total']:.1f}")
        if mode == "guillotine":
            m[1].metric("Safe this week", f"{o['advance']*100:.0f}%",
                        f"{o['eliminated']*100:.0f}% eliminated",
                        delta_color="inverse")
        elif mode == "head_to_head" and o.get("win") is not None:
            m[1].metric("Win probability", f"{o['win']*100:.0f}%",
                        f"vs {o['opponent']}")
        elif mode == "field":
            m[1].metric("Projected rank", f"{o['projected_rank']} of {o['of']}",
                        f"{o.get('beat_median',0)*100:.0f}% to beat median")
        else:
            m[1].metric("Odds", "—")
        m[2].metric("Unavailable", len(rep["unavailable"]))
        m[3].metric("Waiver adds worth a claim", len(rep["cands"]))

        if mode == "guillotine":
            st.warning(
                f"**Guillotine — the lowest scorer is eliminated, so this is "
                f"survival, not a matchup.** Full lineup projects "
                f"**{o['mine']['proj']}** (± {o['mine']['sd']}), "
                f"**{o['projected_rank']} of {o['of']}** in the league. "
                f"Typical week you finish **{o['median_cushion']} points** clear "
                f"of last; in a bad one (10th percentile) only "
                f"**{o['cushion_10th_pct']}**. Rank is not the thing to watch — "
                f"being last is.")
        elif mode == "head_to_head" and o.get("win") is not None:
            st.info(
                f"**{o['mine']['proj']}** (± {o['mine']['sd']}) against "
                f"**{o['opponent']}**'s **{o['opponent_proj']}** — "
                f"**{o['win']*100:.0f}%** to win. Spreads are measured from "
                f"nflverse 2019-25, so this prices how wild a fantasy week "
                f"actually is rather than treating the projection as fact.")
        elif o.get("error"):
            st.caption(f"odds unavailable — {o['error']}")

        with st.expander("How the league projects this week"):
            st.dataframe(pd.DataFrame([{
                "Team": d["team"] + (" (you)" if d["mine"] else ""),
                "Projected": d["proj"], "±": d["sd"],
            } for d in o.get("teams", [])]), hide_index=True, width="stretch")
            st.caption("Variances are added as if players never move together. "
                       "A stacked lineup (your QB and his own receiver) is "
                       "genuinely wider than this shows, so these probabilities "
                       "read slightly overconfident — most of all for stacks.")

        st.subheader("Start")
        st.dataframe(pd.DataFrame([{
            "Slot": slot, "Player": p["name"], "Pos": p["pos_rank"],
            "Team": p.get("team") or "", "Opp": p.get("opponent", ""),
            "Proj": p["week_points"],
            "Flag": ("⚠ " + p["why"]) if p["status"] == "risk" else "",
        } for slot, ps in rep["filled"].items() for p in ps]),
            hide_index=True, width="stretch")

        if rep["calls"]:
            st.caption("**Too close to call:** " + " · ".join(
                f"{c['slot']} — {c['starting']['name']} over "
                f"{c['alternative']['name']} (gap {c['gap']})"
                for c in rep["calls"][:4])
                + "  — inside a projection's noise; use your own read.")

        st.subheader("Waivers")
        if not rep["live"]:
            st.caption(f"⚠ No {2026} game data yet, so usage is measured over "
                       f"**{rep['season']} through week {rep['through']}**. "
                       "This switches to live data by itself once games are played.")
        if rep["cands"] and "error" in rep["cands"][0]:
            st.error(f"usage data unavailable — {rep['cands'][0]['error']}")
        elif not rep["cands"]:
            st.success("Nothing on the wire improves your **starting** lineup — "
                       "don't spend a claim on lineup value alone. With a full "
                       "healthy roster that is the normal answer: a player who "
                       "doesn't crack your starters adds zero by definition.")
        else:
            st.dataframe(pd.DataFrame(rep["cands"]).drop(columns=["_g"]),
                         hide_index=True, width="stretch")

        if rep.get("risers"):
            st.markdown("**Usage risers** — opportunity moves before production, "
                        "so these are the speculative adds worth a bench spot.")
            st.dataframe(pd.DataFrame(rep["risers"]).drop(columns=["_d"]),
                         hide_index=True, width="stretch")

        with st.expander("Bench and droppable"):
            st.dataframe(pd.DataFrame([{
                "Player": p["name"], "Pos": p["pos_rank"],
                "Team": p.get("team") or "", "Opp": p.get("opponent", ""),
                "Proj": p["week_points"],
                "Status": p["status"].upper() if p["status"] != "ok" else "",
            } for p in sorted(rep["bench"], key=lambda x: -x["week_points"])]),
                hide_index=True, width="stretch")
            st.caption("Lowest value rostered: " + ", ".join(
                f"{r['name']} ({round(r['vorp'])})" for r in rep["drops"]))


# ---- All leagues ----------------------------------------------------------
# One screen for five teams. The per-league Report answers "what do I do here";
# this answers the question you actually have on a Sunday morning -- which of
# my five needs me at all.
URGENT_HELP = (
    "Flagged when something needs a decision from you: a starter ruled out or "
    "on bye, a starting slot with nobody in it, or elimination risk in the "
    "guillotine league. A close call is not urgent -- it is two players inside "
    "a projection's noise, and either is defensible."
)


def _flags(rep) -> list[str]:
    """What actually needs you. Empty list means the lineup is fine as it sits."""
    out = []
    if not rep.get("drafted"):
        return ["not drafted"]
    starters = [p for ps in rep["filled"].values() for p in ps]
    for p in starters:
        if p.get("status") in ("out", "unknown"):
            out.append(f"{p['name']} {p.get('status','').upper()}"
                       + (f" — {p['why']}" if p.get("why") else ""))
        elif (p.get("week_points") or 0) <= 0 and p["position"] != "DST":
            out.append(f"{p['name']} projects 0 (bye or inactive)")
    # An unfilled seat scores nothing, which is the most expensive thing here.
    L_, _r, _m = hydrate(rep["league"])
    for slot, want in L_.starters.items():
        got = len(rep["filled"].get(slot, []))
        if got < want:
            out.append(f"no starter at {slot} ({got}/{want})")
    o = rep.get("odds") or {}
    if o.get("mode") == "guillotine" and o.get("eliminated", 0) >= 0.12:
        out.append(f"elimination risk {o['eliminated']*100:.0f}%")
    return out


with t_dash:
    st.header("All leagues")
    wk_all = _default_week()
    st.caption(f"Week {wk_all}. Every league you have drafted, on one line. "
               "Projections are scored under each league's own rules, so the "
               "totals are not comparable across leagues — the odds are.")

    rows_out, flagged = [], []
    for lg in leagues:
        try:
            r = build_report(lg.name, wk_all)
        except Exception as e:
            rows_out.append({"League": lg.name, "Odds": "error", "Projected": None,
                             "Needs you": f"{type(e).__name__}"})
            continue
        if not r.get("drafted"):
            rows_out.append({"League": lg.name, "Odds": "—", "Projected": None,
                             "Opponent / mode": "not drafted", "Needs you": "—"})
            continue
        r = {**r, "league": lg.name}
        o = r.get("odds") or {}
        mode = o.get("mode")
        if mode == "guillotine":
            odds_s, opp = f"{o['advance']*100:.0f}% safe", "guillotine"
        elif mode == "head_to_head" and o.get("win") is not None:
            odds_s, opp = f"{o['win']*100:.0f}% win", f"vs {o['opponent']}"
        elif mode == "field":
            odds_s, opp = f"#{o['projected_rank']} of {o['of']}", "no matchup"
        else:
            odds_s, opp = "—", ""
        f = _flags(r)
        if f:
            flagged.append((lg.name, f))
        rows_out.append({"League": lg.name, "Odds": odds_s,
                         "Projected": round(r["total"], 1),
                         "Opponent / mode": opp,
                         "Needs you": f"⚠ {len(f)}" if f else "clear"})

    st.dataframe(pd.DataFrame(rows_out), hide_index=True, width="stretch")

    st.subheader("Needs you")
    st.caption(URGENT_HELP)
    if not flagged:
        st.success("Nothing needs a decision. Every drafted lineup is legal, "
                   "everyone projected is playing, and no elimination risk.")
    for nm, items in flagged:
        with st.container(border=True):
            st.markdown(f"**{nm}**")
            for i in items:
                st.markdown(f"- {i}")


# ---- Sunday ---------------------------------------------------------------
# Was two tabs, "Sunday" and "Watch", asking one question: what is on and who
# of mine is in it. Merged.
with t_sunday:
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


# ---- Lineup ---------------------------------------------------------------
with t_lineup:
    st.header("Set your lineup")
    st.caption("Weekly projections from both sources, blended and scored under this "
               "league's rules. Players who are out or on bye are excluded from the "
               "lineup, not just flagged.")

    if True:
        from ff import lineup as lineup_mod
        # `league_id` used to be recomputed here from a "replay last season"
        # toggle that defaulted to ON -- but board_rosters() reads the live
        # league regardless, so the toggle only ever moved the WEEK, silently
        # scoring this year's roster against week 10 of a season not yet played.
        wk = int(st.slider("Week", 1, 18, _default_week(), key="lp_wk"))

        blob = get_blob()
        by_key = {key(r["name"], r["position"]): r for r in rows}
        # Raw roster, not board rows: board rows have no kicker and no defense,
        # so this tab used to render a lineup two starting slots short.
        _teams = rosters_mod.all_teams(L, wk, blob)
        _me = next((t for t in _teams if t.mine), None)
        mine = (lineup_mod.build_pool(L, _me.players, wk, by_key, blob)
                if _me else [])

        if not mine:
            st.info("No roster yet — this fills in after your draft.")
        else:
            with st.spinner("scoring the week…"):
                filled, bench = lineup_mod.optimize(mine, L)
                calls = lineup_mod.close_calls(filled, bench, L)
                outdoor = lineup_mod.outdoor_games(wk)

            total = sum(p["week_points"] for ps in filled.values() for p in ps)
            m = st.columns(3)
            m[0].metric("Projected", f"{total:.1f}")
            m[1].metric("Unavailable",
                        sum(1 for p in mine if p["status"] in ("out", "unknown")))
            m[2].metric("Close calls", len(calls))

            st.subheader("Start")
            st.dataframe(pd.DataFrame([{
                "Slot": slot, "Player": p["name"], "Pos": p["pos_rank"],
                "Team": p.get("team") or "", "Opp": p.get("opponent", ""),
                "Proj": p["week_points"], "Spread": p.get("wk_spread", 0),
                "Flag": ("⚠ " + p["why"]) if p["status"] == "risk" else "",
                "Venue": "outdoors" if outdoor.get(p.get("team")) else "dome",
            } for slot, ps in filled.items() for p in ps]),
                hide_index=True, width='stretch')

            st.subheader("Bench")
            st.dataframe(pd.DataFrame([{
                "Player": p["name"], "Pos": p["pos_rank"],
                "Team": p.get("team") or "", "Opp": p.get("opponent", ""),
                "Proj": p["week_points"],
                "Status": p["status"].upper() if p["status"] != "ok" else "",
                "Reason": p["why"],
            } for p in sorted(bench, key=lambda x: -x["week_points"])],),
                hide_index=True, width='stretch')

            if calls:
                st.subheader("Too close to call automatically")
                for c in calls[:6]:
                    st.markdown(
                        f"- **{c['slot']}**: starting **{c['starting']['name']}** "
                        f"({c['starting']['week_points']:.1f}) over "
                        f"**{c['alternative']['name']}** "
                        f"({c['alternative']['week_points']:.1f}) — gap "
                        f"**{c['gap']}**")
                st.caption("Within 1.5 points is inside the noise of a projection. "
                           "Use matchup, weather, or your own read to break these.")

            windy = [p for ps in filled.values() for p in ps
                     if outdoor.get(p.get("team")) and p["position"] in ("QB", "TE", "WR")]
            if windy:
                st.caption("Playing outdoors: " +
                           ", ".join(f"{p['name']} ({p.get('team')})" for p in windy) +
                           " — check wind before kickoff; above ~15mph it hurts the "
                           "passing game meaningfully.")


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
            st.success("Nothing on the wire improves your lineup. Don't spend a claim.")
        else:
            st.dataframe(pd.DataFrame(useful).drop(columns=["_g"]),
                         hide_index=True, width='stretch')

        st.subheader("Usage risers — opportunity moves before production")
        movers = sorted([c for c in cands], key=lambda c: -float(c["ΔSnap"]))[:12]
        st.dataframe(pd.DataFrame(movers)[
            ["Player", "Pos", "Snap%", "ΔSnap", "Tgt", "ΔTgt", "PPG"]],
            hide_index=True, width='stretch')

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
with t_keep:
    st.header("Keepers")
    k = cfg.get("keepers")
    if not k or not cfg.get("owner_id"):
        st.info("This league has no keeper settings configured in leagues.yaml.")
    else:
        prev = L.raw.get("previous_league_id")
        if not prev:
            st.warning("No previous season on file — nothing to keep from.")
        else:
            blob = get_blob()
            cands = keeper_mod.roster_and_costs(
                prev, cfg["owner_id"], blob,
                escalation=k.get("escalation", 1),
                undrafted_round=k.get("undrafted_round"),
                round_one_keepable=k.get("round_one_keepable", True))
            cands = [c for c in cands if c["position"] not in ("K", "DEF", "DST")]
            res = keeper_mod.value(cands, rows, L)
            keep = [r for r in res if (r.get("surplus") or 0) > 0][: k["max"]]

            st.caption(f"Keep up to **{k['max']}**; a kept player costs a pick "
                       f"**{k['escalation']} round earlier** than drafted. "
                       f"Round-1 picks "
                       f"{'are keepable' if k.get('round_one_keepable') else 'are **not** keepable'}.")
            if keep:
                st.success("Keep: " + " · ".join(
                    f"**{r['name']}** (rd{r['cost_round']}, {r['surplus']:+.0f})" for r in keep))
                if len(keep) < k["max"]:
                    st.warning(f"Only {len(keep)} of {k['max']} clear zero. Don't fill the "
                               "rest — keeping a negative-surplus player is worse than "
                               "using the pick.")
            else:
                st.info("Nobody is worth his keeper cost. Draft clean.")

            st.dataframe(pd.DataFrame([{
                "Keep": "✓" if r in keep else "",
                "Player": r["name"], "Pos": r.get("pos_rank", r["position"]),
                "Cost": f"rd{r['cost_round']}" if r.get("cost_round") else "—",
                "2026 VORP": round(r["vorp"]) if r.get("vorp") is not None else None,
                "Alt at that pick": r.get("alt") or "—",
                "Surplus": round(r["surplus"]) if r.get("surplus") is not None else None,
                "Note": r.get("note", ""),
            } for r in res]), hide_index=True, width='stretch')


