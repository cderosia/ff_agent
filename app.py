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
st.sidebar.caption(
    "Preseason: rosters and drafts are empty until they happen. "
    "Waivers, Lineup and Trades offer a replay of last season so you can see "
    "real output."
)
st.sidebar.info(
    "**Draft day** runs separately:\n\n`python3 scripts/draft_server.py`\n\n"
    "It's a glanceable second screen that refreshes itself, with its own "
    "league switcher."
)


# ---------------------------------------------------------------------------
# tabs
# ---------------------------------------------------------------------------
(t_sunday, t_watch, t_board, t_lineup, t_waiver, t_trade,
 t_keep, t_report) = st.tabs(
    ["Sunday", "Watch", "Board", "Lineup", "Waivers", "Trades",
     "Keepers", "Report"])


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


def _default_week() -> int:
    ls, _ = leagues_objects()          # returns (leagues, errors)
    for l in ls:
        w = l.raw.get("current_week")
        if w:
            return int(w)
    return 1


# ---- Sunday ---------------------------------------------------------------
with t_sunday:
    from ff import live as live_mod
    st.header("Sunday")
    c1, c2, c3 = st.columns([1, 1, 3])
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
    live_now = [g for g in gs if g["state"] == "in"]
    if live_now:
        st.subheader(f"In progress ({len(live_now)})")
    for g in (live_now or gs):
        mine_here = hold.get(g["home"], []) + hold.get(g["away"], [])
        if not mine_here and g["state"] != "in":
            continue
        head = (f"**{g['away']} {g['away_score']} — {g['home_score']} {g['home']}**"
                if g["state"] != "pre" else
                f"**{g['away']} @ {g['home']}**")
        st.markdown(f"{head}  ·  {g['detail']}  ·  {g['network']}")
        if mine_here:
            st.dataframe(
                [{"player": h["player"], "pos": h["position"],
                  "league": h["league"],
                  "role": "START" if h["starter"] else "bench",
                  "proj": h["proj"]} for h in
                 sorted(mine_here, key=lambda h: (not h["starter"], -h["proj"]))],
                hide_index=True, width="stretch")
        st.divider()


# ---- Watch ----------------------------------------------------------------
with t_watch:
    from ff import live as live_mod
    st.header("What to put on")
    st.caption("One pick per time slot — you can only watch one game at a time. "
               "Starters only: a bench player's points aren't yours this week. "
               "A player you roster in three leagues counts three times. Ties go "
               "to projected points.")
    wk2 = int(st.number_input("Week", 1, 18, _default_week(), key="watch_wk"))
    try:
        slots = live_mod.watch_by_slot(_games(wk2), _holdings(wk2))
    except Exception as e:
        st.error(f"{type(e).__name__}: {e}")
        slots = []

    if not slots:
        st.info("No games have your starters in them yet.")
    for s_ in slots:
        g = s_["pick"]
        st.subheader(s_["slot"])
        st.markdown(
            f"**{g['away']} @ {g['home']}** on **{g['network']}** — "
            f"{g['n_players']} starter{'s' if g['n_players'] != 1 else ''}, "
            f"{g['proj_total']} projected points")
        st.dataframe(
            [{"player": h["player"], "pos": h["position"], "proj": h["proj"],
              "league": h["league"], "team": h["nfl_team"]} for h in g["players"]],
            hide_index=True, width="stretch")
        if s_["others"]:
            with st.expander(f"other games this slot ({len(s_['others'])})"):
                st.dataframe(
                    [{"game": f"{o['away']} @ {o['home']}", "on": o["network"],
                      "starters": o["n_players"], "proj": o["proj_total"]}
                     for o in s_["others"]],
                    hide_index=True, width="stretch")
        st.divider()


# ---- Board ----------------------------------------------------------------
with t_board:
    st.header(f"{L.name} — draft board")
    c = st.columns(4)
    c[0].metric("Players", len(rows))
    c[1].metric("Market", meta["source"], f"{meta['match_rate']:.0%} matched")
    c[2].metric("Starters", L.starter_slots, f"+{L.bench} bench")
    c[3].metric("Replacement RB / WR",
                f"{L.replacement.get('RB',0):.0f} / {L.replacement.get('WR',0):.0f}")

    left, right = st.columns(2)
    with left:
        st.subheader("Value — draft later than market")
        v = board_mod.values(rows, 12)
        st.dataframe(pd.DataFrame([{
            "Player": r["name"], "Pos": r["pos_rank"],
            "You": r["your_rank"], "Market": r["adp_rank"], "Edge": r["edge"],
        } for r in v]), hide_index=True, width='stretch')
    with right:
        st.subheader("Reach — let the room overpay")
        rr = board_mod.reaches(rows, 12)
        st.dataframe(pd.DataFrame([{
            "Player": r["name"], "Pos": r["pos_rank"],
            "You": r["your_rank"], "Market": r["adp_rank"], "Edge": r["edge"],
        } for r in rr]), hide_index=True, width='stretch')

    st.subheader("Full board")
    pos_filter = st.multiselect("Position", ["QB", "RB", "WR", "TE"], [])
    show = [r for r in rows if not pos_filter or r["position"] in pos_filter]
    st.dataframe(pd.DataFrame([{
        "#": r["vbd_rank"], "Player": r["name"], "Pos": r["pos_rank"],
        "Proj": round(r["points"]), "VORP": round(r["vorp"]),
        "Market": r.get("adp_rank"), "Edge": r.get("edge"),
        "Src": r.get("n_sources", 1),
        "Disagree": f"{r.get('rel_spread',0)*100:.0f}%" if r.get("rel_spread") else "",
    } for r in show[:250]]), hide_index=True, width='stretch', height=520)


# ---- Lineup ---------------------------------------------------------------
with t_lineup:
    st.header("Set your lineup")
    st.caption("Weekly projections from both sources, blended and scored under this "
               "league's rules. Players who are out or on bye are excluded from the "
               "lineup, not just flagged.")

    if True:
        from ff import lineup as lineup_mod
        c1, c2 = st.columns([1, 3])
        lp_replay = c1.toggle("Replay a past week", value=True, key="lp_rep")
        wk = int(c2.slider("Week", 1, 18, 10, key="lp_wk"))
        league_id = L.raw.get("previous_league_id") if lp_replay else L.league_id

        blob = get_blob()
        by_key = {key(r["name"], r["position"]): r for r in rows}
        mine, _ = rosters_mod.board_rosters(L, by_key, wk, blob)

        if not mine:
            st.info("No roster yet — this fills in after your draft.")
        else:
            with st.spinner("scoring the week…"):
                wp = lineup_mod.weekly_points(wk, L.scoring)
                for pl in mine:
                    pts, n, sp = wp.get(key(pl["name"], pl["position"]), (0.0, 0, 0.0))
                    pl["week_points"] = round(pts, 1)
                    pl["wk_spread"] = round(sp, 1)
                    pl["status"], pl["why"] = lineup_mod.availability(
                        blob, pl["name"], pl["position"], pl.get("team"), wk, pts)
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
                "Team": p.get("team") or "", "Proj": p["week_points"],
                "Spread": p.get("wk_spread", 0),
                "Flag": ("⚠ " + p["why"]) if p["status"] == "risk" else "",
                "Venue": "outdoors" if outdoor.get(p.get("team")) else "dome",
            } for slot, ps in filled.items() for p in ps]),
                hide_index=True, width='stretch')

            st.subheader("Bench")
            st.dataframe(pd.DataFrame([{
                "Player": p["name"], "Pos": p["pos_rank"],
                "Team": p.get("team") or "", "Proj": p["week_points"],
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
        replay = st.toggle("Replay last season (real data — rosters are empty preseason)",
                           value=True)
        season = 2025 if replay else 2026
        week = st.slider("Week", 4, 17, 10) if replay else 1

        with st.spinner("crunching usage trends…"):
            blob = get_blob()
            league_id = L.raw.get("previous_league_id") if replay else L.league_id
            trend = weekly_mod.usage_trend(season, week - 1)
            trend["k"] = [key(n, p) for n, p in
                          zip(trend.player_display_name, trend.position)]
            ppg = weekly_mod.recent_points(season, week - 1, L.scoring)
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


# ---- Report ---------------------------------------------------------------
with t_report:
    st.header("Weekly report")
    st.caption("The assembled digest — the same thing that gets emailed. "
               "Tuesday covers claims before waivers run; Wednesday covers "
               "lineups and trades once they've cleared.")

    if L.platform != "sleeper" or not cfg.get("owner_id"):
        st.info("Report generation currently needs a Sleeper league with owner_id set.")
    else:
        c1, c2, c3 = st.columns([1, 1, 2])
        kind = c1.radio("Which report", ["Tuesday — waivers", "Wednesday — lineup"],
                        key="rkind")
        replay = c2.toggle("Replay a past week", value=True, key="rep")
        season = 2025 if replay else 2026
        week = c3.number_input("Week", 4, 17, 10) if replay else 1

        if st.button("Build report", type="primary"):
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "send_weekly", ROOT / "scripts" / "send_weekly.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            from ff import notify
            with st.spinner("building…"):
                claims, movers, fair, drops = mod.gather(L, cfg, season, int(week), replay)
                if kind.startswith("Wednesday"):
                    html = mod.build_lineup_email(L, cfg, season, int(week),
                                                  replay, fair)
                else:
                    html = notify.render_email(L, int(week), claims, movers, fair,
                                               drops, replay)
            st.session_state["report_html"] = html
            st.session_state["report_meta"] = (L.name, int(week))

        if st.session_state.get("report_html"):
            html = st.session_state["report_html"]
            name, wk = st.session_state["report_meta"]
            st.components.v1.html(html, height=900, scrolling=True)
            d1, d2 = st.columns([1, 3])
            d1.download_button("Download HTML", html,
                               file_name=f"{name}-w{wk}.html", mime="text/html")
            if d2.button("Email it to me now"):
                from ff import notify
                try:
                    top = "report"
                    st.success(notify.send(f"Week {wk} · {name}", html))
                except Exception as e:
                    st.error(f"{e}  —  set SMTP_USER / SMTP_PASS / REPORT_TO in .env")
