#!/usr/bin/env python3
"""Draft-day board. One job, no chrome.

    python3 scripts/draft_server.py
    open http://localhost:8777

Serves every configured league behind a tab strip, so you can switch leagues
without restarting. Deliberately separate from the season app: on draft day you
want a glanceable second screen that refreshes itself, not a tool to operate.

Picks are polled on demand per league and cached for a few seconds, so an idle
league costs nothing and switching is instant.

Practice mode rehearses draft day against bots:

    python3 scripts/draft_server.py --sim work --slot 5 --rounds 3
    python3 scripts/draft_server.py --sim work --slot 5 --transcribe

The room drafts itself off jittered ADP while a clock runs on you; let it expire
and it autopicks, exactly like the platform would. `--transcribe` adds the part
that actually bites in a league with no live feed: every pick the room makes has
to be typed into the board before the next one lands. Practice picks are written
to `_sim_<league>.json`, never the real pick file.
"""
from __future__ import annotations

import argparse
import collections
import functools
import json
import pathlib
import random
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ff import board as board_mod        # noqa: E402
from ff import draft as draft_mod        # noqa: E402
from ff import vbd as vbd_mod            # noqa: E402
from ff import special as special_mod    # noqa: E402
from ff import starts as starts_mod      # noqa: E402
from ff import why as why_mod            # noqa: E402
from ff.leagues import _env, configured_slot, load_all   # noqa: E402
from ff.names import key, normalize      # noqa: E402
from ff.projections import fetch         # noqa: E402

LOCK = threading.Lock()
INGEST_TS = {}
KEEPER_CACHE = {}
# Keepers can be declared any time before a draft, so refresh on a slow timer.
KEEPER_TTL = 60.0
BYES = starts_mod.bye_by_team()
PICKS_DIR = ROOT / "data" / "manual_picks"
CTX: dict = {}          # league name -> prepared board + identity
PICK_CACHE: dict = {}   # league name -> (timestamp, picks)
POLL_SECONDS = 1.0
# Near your turn, staleness costs you clock, so both the server cache and the
# browser poll tighten further when your pick is close. Sleeper answers in
# ~0.1s and this is one request per second for the league you're looking at --
# roughly 60/min against a public API that tolerates far more.
POLL_SECONDS_HOT = 0.5
HOT_WITHIN = 3          # picks away from your turn
SIM: dict = {}          # league name -> practice-draft state (see sim_start)


# ---------------------------------------------------------------------------
def my_espn_team(league_id, cookies):
    swid = _env("ESPN_SWID")
    url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026"
           f"/segments/0/leagues/{league_id}?view=mTeam")
    d = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                     cookies=cookies, timeout=25).json()
    me = next((m["id"] for m in d.get("members", [])
               if m.get("id", "").upper() == swid.upper()), None)
    for t in d.get("teams", []):
        if me and me in (t.get("owners") or []):
            return t["id"]
    return None


def snake_picks_for_slot(slot, teams, rounds):
    return [(rd - 1) * teams + (slot if rd % 2 else teams - slot + 1)
            for rd in range(1, rounds + 1)]


def prepare(leagues, cfgs, proj):
    """Build each league's board once at startup."""
    for L in leagues:
        cfg = cfgs.get(L.name, {})
        rows, meta = board_mod.build(proj, L)
        if L.platform == "espn":
            ck = {"espn_s2": _env("ESPN_S2"), "SWID": _env("ESPN_SWID")}
            try:
                me = my_espn_team(L.league_id, ck)
            except Exception:
                me = None
        elif L.platform == "sleeper":
            me = cfg.get("owner_id")
        else:
            me = "me"          # manual entry tags your own picks directly
        if cfg.get("ingest"):
            # Scraped picks are tagged "me"/"other" by the draft room's own
            # myTeam marker, not by platform team id.
            me = "me"

        # An explicitly configured slot WINS. The platform is only asked when
        # leagues.yaml doesn't say. Previously the API answer overwrote the
        # config unconditionally, so a Sleeper league with no published draft
        # order silently reset a configured slot to None, and ESPN's stale
        # order quietly contradicted what the user had told us.
        slot = configured_slot(cfg)
        try:
            if slot or L.platform not in ("sleeper", "espn"):
                pass
            elif L.platform == "sleeper":
                d = requests.get(
                    f"https://api.sleeper.app/v1/draft/{L.raw['draft_id']}",
                    timeout=15).json()
                order = d.get("draft_order") or {}
                slot = order.get(str(me)) or order.get(me)
            elif me:
                ck = {"espn_s2": _env("ESPN_S2"), "SWID": _env("ESPN_SWID")}
                rd1 = draft_mod.espn_draft_order(L.league_id, ck)
                slot = next((pk for pk, tid in rd1.items() if tid == me), None)
        except Exception:
            pass

        # Keepers are already on your roster before pick 1, and the rounds
        # they cost are picks you no longer hold. Without both, the board
        # offers you players you own and plans around picks you don't have.
        keep_cfg = ((cfg.get("keepers") or {}).get("keep")) or []
        by_norm = {normalize(r["name"]): r for r in rows}
        keepers_rows, keeper_rounds, missing = [], [], []
        for k in keep_cfg:
            row = by_norm.get(normalize(k["name"]))
            if row is None:
                missing.append(k["name"])
                continue
            keepers_rows.append(row)
            if k.get("round"):
                keeper_rounds.append(int(k["round"]))
        if missing:
            print(f"  {L.name}: keeper not found on board: {', '.join(missing)}")

        CTX[L.name] = {"league": L, "rows": rows, "meta": meta, "me": me,
                       "slot": slot, "rounds": L.starter_slots + L.bench,
                       "keepers": keepers_rows, "keeper_rounds": keeper_rounds,
                       "ingest": bool(cfg.get("ingest")),
                       # K and DST are kept off the main board on purpose (see
                       # ff.special), but a board that omits two starting slots
                       # is how you reach the last round with neither filled.
                       "dst": (special_mod.dst_early(3)
                               if L.starters.get("DST") else []),
                       "kickers": (special_mod.kickers(proj, L)[:12]
                                   if L.starters.get("K") else []),
                       "id_map": {p["espn_id"]: p for p in proj if p.get("espn_id")}}
        print(f"  {L.name:16} {L.teams:>2}tm {L.platform:8} slot {slot or '?'} "
              f"· {meta['source']}")


def manual_path(name):
    # A practice draft writes to its own file so a rehearsal can never be
    # mistaken for, or overwrite, the real draft you enter on the day.
    if name in SIM:
        return PICKS_DIR / f"_sim_{name}.json"
    return PICKS_DIR / f"{name}.json"


def manual_picks(name):
    """Picks entered by hand, for leagues with no readable draft feed."""
    p = manual_path(name)
    return json.loads(p.read_text()) if p.exists() else []


def add_manual_pick(name, player, mine):
    PICKS_DIR.mkdir(parents=True, exist_ok=True)
    picks = manual_picks(name)
    picks.append({"pick_no": len(picks) + 1,
                  "round": len(picks) // CTX[name]["league"].teams + 1,
                  "name": player["name"], "position": player["position"],
                  "by": "me" if mine else "other", "slot": None})
    manual_path(name).write_text(json.dumps(picks))
    PICK_CACHE.pop(name, None)
    return picks


def ingest_picks(name, rows):
    """Replace this league's picks from a scraped draft room.

    ESPN's read API publishes nothing while a draft is live (verified: zero
    picks and zero rostered players across mDraftDetail and mRoster, held for
    40+ seconds mid-draft, while a COMPLETED draft returns all of them). The
    draft room itself has every pick in the DOM, so the browser scrapes it and
    posts it here.

    Deliberately a full replace rather than an append: the scraper re-sends the
    whole board every tick, so a dropped request, a reload or a reconnect
    self-heals on the next one. Appending would drift the moment one was missed.
    """
    board = {key(r["name"], r["position"]): r for r in CTX[name]["rows"]}
    teams = CTX[name]["league"].teams

    # ESPN's draft board grid is laid out COLUMN-MAJOR -- every one of a team's
    # picks, then the next team's -- so DOM order is not draft order. Trusting
    # it made the first N cells look like picks 1..N of the draft, when they
    # were really one manager's whole column. Round and slot come off the cell
    # itself ("3.1"), so derive the real overall number from those.
    def overall(r):
        rd, slot = r.get("round"), r.get("slot")
        if rd and slot:
            return (int(rd) - 1) * teams + int(slot)
        return r.get("pick_no") or 0

    out, unmatched = [], []
    for i, r in enumerate(sorted(rows, key=overall), 1):
        nm, pos = r.get("name") or "", r.get("position")
        row = board.get(key(nm, pos))
        if row is None:                      # K/DST aren't on our board at all
            unmatched.append(nm)
        out.append({"pick_no": overall(r) or i,
                    "round": r.get("round") or ((i - 1) // teams + 1),
                    "name": row["name"] if row else nm,
                    "position": row["position"] if row else pos,
                    "by": "me" if r.get("mine") else "other",
                    "slot": None})
    PICKS_DIR.mkdir(parents=True, exist_ok=True)
    manual_path(name).write_text(json.dumps(out))
    PICK_CACHE.pop(name, None)
    INGEST_TS[name] = time.time()
    return out, unmatched


def undo_manual_pick(name):
    picks = manual_picks(name)[:-1]
    manual_path(name).write_text(json.dumps(picks))
    PICK_CACHE.pop(name, None)
    return picks


def is_manual(name):
    # A practice draft is its own feed, so it reads from the local pick file even
    # for a league that normally polls ESPN or Sleeper. So is a league fed by the
    # browser scraper -- there is no API to poll for it.
    return (name in SIM
            or CTX[name].get("ingest")
            or CTX[name]["league"].platform not in ("sleeper", "espn"))


def picks_for(name, hot=False):
    """This league's picks, cached briefly so switching is cheap."""
    if is_manual(name):
        return manual_picks(name)          # local file; no polling
    now = time.time()
    ttl = POLL_SECONDS_HOT if hot else POLL_SECONDS
    ts, cached = PICK_CACHE.get(name, (0, None))
    if cached is not None and now - ts < ttl:
        return cached
    c = CTX[name]
    L = c["league"]
    if L.platform == "sleeper":
        picks = draft_mod.sleeper_picks(L.raw["draft_id"])
    else:
        ck = {"espn_s2": _env("ESPN_S2"), "SWID": _env("ESPN_SWID")}
        picks = draft_mod.espn_picks(L.league_id, ck, c["id_map"])
    PICK_CACHE[name] = (now, picks)
    return picks


# ---------------------------------------------------------------------------
# Practice draft
#
# The point is rehearsing the thing that actually goes wrong on draft day: for a
# league with no live feed, YOU are the feed, and every pick in the room has to
# be typed in while a clock runs. So this drives the real UI and the real manual
# entry path rather than a lookalike -- the only fiction is that the other nine
# managers are bots.
# ---------------------------------------------------------------------------
BOT_CAPS = {"QB": 1, "RB": 4, "WR": 4, "TE": 1}   # before the late rounds


def slot_of_pick(pick_no: int, teams: int) -> int:
    """Which draft slot owns `pick_no` in a snake draft."""
    rnd = (pick_no - 1) // teams + 1
    i = (pick_no - 1) % teams
    return i + 1 if rnd % 2 == 1 else teams - i


def sim_start(name, slot, rounds, clock, bot_secs, transcribe=False,
              entry_secs=20, seed=None):
    L = CTX[name]["league"]
    SIM[name] = {
        "slot": slot, "rounds": rounds, "clock": clock, "bot_secs": bot_secs,
        "total": rounds * L.teams, "deadline": None, "next_bot": 0.0,
        "done": False, "autopicked": [], "rng": random.Random(seed),
        # transcribe drill
        "transcribe": transcribe, "entry_secs": entry_secs,
        "pending": None, "entry_deadline": 0.0, "missed": [], "typed": [],
    }
    manual_path(name).parent.mkdir(parents=True, exist_ok=True)
    manual_path(name).write_text("[]")
    PICK_CACHE.pop(name, None)


def sim_pick_choice(name):
    """Who the bot on the clock takes -- chosen, not yet committed.

    Sampling ADP with its own published spread is what makes the room feel real --
    players slide and get reached for by about as much as they actually do,
    instead of coming off in a rigid ADP order you could just memorise.
    """
    c, L = CTX[name], CTX[name]["league"]
    s = SIM[name]
    picks = manual_picks(name)
    taken = {key(p["name"], p["position"]) for p in picks}
    pick_no = len(picks) + 1
    bot_slot = slot_of_pick(pick_no, L.teams)
    rnd = (pick_no - 1) // L.teams + 1

    have = {}
    for i, p in enumerate(picks, 1):
        if slot_of_pick(i, L.teams) == bot_slot:
            have[p["position"]] = have.get(p["position"], 0) + 1

    best, best_score = None, None
    for r in c["rows"]:
        if key(r["name"], r["position"]) in taken:
            continue
        cap = BOT_CAPS.get(r["position"])
        if rnd <= 8 and cap is not None and have.get(r["position"], 0) >= cap:
            continue
        adp = r.get("adp") or 999.0
        sd = r.get("adp_sd") or 8.0
        score = adp + s["rng"].gauss(0, sd)
        if best_score is None or score < best_score:
            best, best_score = r, score
    return best


def sim_bot_pick(name):
    """Choose and commit one bot pick."""
    best = sim_pick_choice(name)
    if best is None:
        return None
    add_manual_pick(name, best, mine=False)
    return best


def sim_autopick(name):
    """Clock expired -- take the top recommendation, exactly like the platform would."""
    c, L = CTX[name], CTX[name]["league"]
    picks = manual_picks(name)
    taken = {key(p["name"], p["position"]) for p in picks}
    by_key = {key(r["name"], r["position"]): r for r in c["rows"]}
    mine = [by_key[key(p["name"], p["position"])] for i, p in enumerate(picks, 1)
            if slot_of_pick(i, L.teams) == SIM[name]["slot"]
            and key(p["name"], p["position"]) in by_key]
    on_clock = len(picks) + 1
    my_picks = snake_picks_for_slot(SIM[name]["slot"], L.teams, SIM[name]["rounds"])
    later = [p for p in my_picks if p > on_clock]
    recs = draft_mod.recommend(c["rows"], L, taken, mine, limit=1,
                               next_pick=on_clock,
                               following_pick=later[0] if later else None)
    if recs:
        add_manual_pick(name, recs[0], mine=True)
        SIM[name]["autopicked"].append(recs[0]["name"])
        return recs[0]
    return None


def sim_advance(name):
    """Move the practice draft forward. Called on a timer; cheap when idle."""
    s = SIM.get(name)
    if not s or s["done"]:
        return
    L = CTX[name]["league"]
    picks = manual_picks(name)
    if len(picks) >= s["total"]:
        s["done"], s["deadline"] = True, None
        return

    on_clock = len(picks) + 1
    now = time.time()
    if slot_of_pick(on_clock, L.teams) == s["slot"]:
        if s["deadline"] is None:              # your turn just came around
            s["deadline"] = now + s["clock"]
        elif now >= s["deadline"]:
            sim_autopick(name)
            s["deadline"] = None
            s["next_bot"] = now + s["bot_secs"]
        return

    s["deadline"] = None

    if not s["transcribe"]:
        if now >= s["next_bot"]:
            sim_bot_pick(name)
            s["next_bot"] = now + s["bot_secs"]
        return

    # Transcribe drill: the room picks, and YOU have to get it into the board
    # before the next one lands -- which is the actual job on draft day in a
    # league with no live feed.
    if s["pending"] is None:
        if now >= s["next_bot"]:
            r = sim_pick_choice(name)
            if r is None:
                s["done"] = True
                return
            s["pending"] = {"name": r["name"], "position": r["position"],
                            "slot": slot_of_pick(on_clock, L.teams)}
            s["entry_deadline"] = now + s["entry_secs"]
    elif now >= s["entry_deadline"]:
        row = next((r for r in CTX[name]["rows"]
                    if r["name"] == s["pending"]["name"]), None)
        if row:
            add_manual_pick(name, row, mine=False)
        s["missed"].append(s["pending"]["name"])
        s["pending"] = None
        s["next_bot"] = now + s["bot_secs"]


def sim_owns(name, mine_flag):
    """In a practice draft the snake decides whose pick it is, not the checkbox."""
    s = SIM.get(name)
    if not s:
        return mine_flag
    on_clock = len(manual_picks(name)) + 1
    return slot_of_pick(on_clock, CTX[name]["league"].teams) == s["slot"]


def sim_accept(name, row):
    """Gate a manual entry during a practice draft. Returns (ok, error).

    In the transcribe drill the room has already made its pick, so entering
    someone else is the mistake worth catching -- on the day it would silently
    corrupt the board and every recommendation after it.
    """
    s = SIM.get(name)
    if not s or not s["transcribe"] or s["pending"] is None:
        return True, None
    if row["name"] == s["pending"]["name"]:
        s["typed"].append(row["name"])
        s["pending"] = None
        s["next_bot"] = time.time() + s["bot_secs"]
        return True, None
    return False, (f"not the pick — team {s['pending']['slot']} took "
                   f"{s['pending']['name']}, you typed {row['name']}")


def sim_loop():
    while True:
        time.sleep(0.4)
        try:
            with LOCK:
                for nm in list(SIM):
                    sim_advance(nm)
        except Exception as e:                 # never let the sim kill the server
            print(f"  ! sim: {type(e).__name__}: {e}")


def sim_state(name):
    s = SIM.get(name)
    if not s:
        return None
    L = CTX[name]["league"]
    picks = manual_picks(name)
    on_clock = len(picks) + 1
    yours = slot_of_pick(on_clock, L.teams) == s["slot"] and not s["done"]
    now = time.time()
    if yours:
        # The timer thread sets the deadline a fraction of a second after your
        # turn arrives; show the full clock rather than a blank in that gap.
        left = max(0, s["deadline"] - now) if s["deadline"] else s["clock"]
    elif s["pending"]:
        left = max(0, s["entry_deadline"] - now)
    else:
        left = None
    return {
        "on": True, "yours": yours, "done": s["done"],
        "left": round(left) if left is not None else None,
        "clock": s["clock"], "slot": s["slot"],
        "round": (on_clock - 1) // L.teams + 1, "rounds": s["rounds"],
        "pick_no": min(on_clock, s["total"]), "total": s["total"],
        "on_slot": slot_of_pick(on_clock, L.teams),
        "autopicked": s["autopicked"],
        "transcribe": s["transcribe"],
        "pending": s["pending"],
        "missed": s["missed"], "typed": len(s["typed"]),
    }


def tier_breaks(rows, position, dyn, n=14):
    """Mark where the cliff is within a position.

    A tier break is a drop to the next player that's much larger than the
    typical gap. Knowing three players remain before a cliff is what actually
    decides whether you take a position now or wait a round.
    """
    at = [r for r in rows if r["position"] == position][:n]
    if len(at) < 3:
        return []
    gaps = [at[i]["points"] - at[i + 1]["points"] for i in range(len(at) - 1)]
    typical = sorted(gaps)[len(gaps) // 2] or 1.0
    # The floor has to scale with the position, not be a fixed number of points.
    # At the top of a board the gaps are naturally big, so a flat threshold makes
    # every elite player his own tier -- which tells you nothing.
    dv = lambda r: dyn.get(key(r["name"], r["position"]), {}).get(
        "dyn_vorp", r["vorp"])
    span = max(1.0, dv(at[0]) - dv(at[-1]))
    threshold = max(typical * 2.2, span * 0.14)
    out, tier = [], 1
    for i, r in enumerate(at):
        last = i < len(gaps) and gaps[i] > threshold
        out.append({"n": r["name"], "p": r["pos_rank"], "v": round(dv(r)),
                    "tier": tier, "last_of_tier": last,
                    "drop": round(gaps[i], 1) if last else None})
        if last:
            tier += 1
    return out


def wait_cost(rows, taken_keys, next_pick, following_pick, dyn, on_clock):
    """Per position: best now vs. best likely to survive to your next pick."""
    out = {}
    for pos in ("QB", "RB", "WR", "TE"):
        at = [r for r in rows if r["position"] == pos
              and key(r["name"], r["position"]) not in taken_keys]
        if not at:
            continue
        dv = lambda r: dyn.get(key(r["name"], r["position"]), {}).get(
            "dyn_vorp", r["vorp"])
        best = max(at, key=dv)
        # Where he goes NEXT, not where he went on paper: a player 40th in the
        # remaining market goes around pick on_clock+39, whatever his preseason
        # ADP rank said before 60 players came off the board.
        def goes_at(r):
            k = dyn.get(key(r["name"], r["position"]), {}).get("dyn_adp_rank")
            return None if k is None else on_clock + k - 1
        survivors = [r for r in at
                     if (goes_at(r) or 0) >= following_pick]
        later = max(survivors, key=dv) if survivors else None
        out[pos] = {
            "now": best["name"], "now_v": round(dv(best)),
            "later": later["name"] if later else None,
            "later_v": round(dv(later)) if later else None,
            "cost": round(dv(best) - dv(later)) if later else None,
        }
    return out


def positional_run(picks, window=8):
    """Is a run happening right now?"""
    recent = [p.get("position") for p in picks[-window:] if p.get("position")]
    if len(recent) < 4:
        return None
    counts = {}
    for x in recent:
        counts[x] = counts.get(x, 0) + 1
    pos, n = max(counts.items(), key=lambda kv: kv[1])
    return {"pos": pos, "n": n, "of": len(recent)} if n >= len(recent) * 0.5 else None


DST_POS = {"DEF", "DST", "D/ST"}


@functools.lru_cache(maxsize=1)
def players_blob():
    """Sleeper's player dictionary, for turning keeper ids into names.

    Read from the cached pull rather than fetched: it's 16MB, it barely
    changes, and draft day is the wrong time to download it.
    """
    path = ROOT / "data" / "raw" / "sleeper_players.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def league_keepers(name):
    """Other managers' declared keepers, cached — they can be set any time.

    Refreshed on a slow timer rather than at startup: a manager declaring a
    keeper an hour before the draft would otherwise never reach a board that
    was already running.
    """
    c = CTX[name]
    L = c["league"]
    if L.platform != "sleeper":
        return []
    now = time.time()
    ts, cached = KEEPER_CACHE.get(name, (0, None))
    if cached is not None and now - ts < KEEPER_TTL:
        return cached
    try:
        out = draft_mod.sleeper_keepers(L.league_id, players_blob())
    except Exception:
        out = cached or []          # a failed refresh must not empty the board
    KEEPER_CACHE[name] = (now, out)
    return out


def special_pool(name, picks, mine_raw):
    """Kickers and defenses still available, plus which you still need.

    The pools are returned whether or not you need one, because they back the
    DST and K tabs on the board -- you want to be able to look before it's
    urgent. `need` is what decides whether the reminder card is shown.
    """
    c = CTX[name]
    L = c["league"]
    taken_txt = " | ".join((p.get("name") or "") for p in picks).upper()

    def gone(label, team=None, nick=None):
        # Match on the nickname ("RAMS"), which shows up whether the feed says
        # "Los Angeles Rams", "LAR D/ST" or "Rams D/ST". Team codes do not.
        if nick and len(nick) > 3 and nick.upper() in taken_txt:
            return True
        if team and f"{team.upper()} " in taken_txt:
            return True
        return label.upper() in taken_txt

    have_dst = any((p.get("position") or "").upper() in DST_POS for p in mine_raw)
    have_k = any((p.get("position") or "").upper() == "K" for p in mine_raw)

    dst = [d for d in c.get("dst") or []
           if not gone(d["name"], d["team"], d.get("nick"))][:12]
    ks = [k for k in c.get("kickers") or [] if not gone(k["name"])][:12]
    need = []
    if L.starters.get("DST") and not have_dst:
        need.append("DST")
    if L.starters.get("K") and not have_k:
        need.append("K")
    return {"need": need, "dst": dst, "k": ks,
            "slots": [x for x in ("DST", "K") if L.starters.get(x)]}


def state_for(name):
    c = CTX[name]
    L, rows = c["league"], c["rows"]
    picks = picks_for(name)
    # If that put us within a few picks of your turn, the cached copy is not
    # good enough -- take the fresh one.
    slot_now = CTX[name].get("slot")
    if slot_now and not SIM.get(name):
        rounds_now = CTX[name]["rounds"]
        mine_at = snake_picks_for_slot(slot_now, CTX[name]["league"].teams, rounds_now)
        nxt = next((q for q in mine_at if q >= len(picks) + 1), None)
        if nxt is not None and nxt - (len(picks) + 1) <= HOT_WITHIN:
            picks = picks_for(name, hot=True)
    taken = {key(p["name"], p["position"]) for p in picks if p["name"]}
    by_key = {key(r["name"], r["position"]): r for r in rows}
    sim = SIM.get(name)
    if sim:
        # The practice draft knows the slot, so ownership is derived from the
        # snake rather than from whether a checkbox got ticked in a hurry.
        mine = [by_key[key(p["name"], p["position"])] for i, p in enumerate(picks, 1)
                if slot_of_pick(i, L.teams) == sim["slot"]
                and key(p["name"], p["position"]) in by_key]
    else:
        mine = [by_key[key(p["name"], p["position"])] for p in picks
                if p.get("by") == c["me"] and key(p["name"], p["position"]) in by_key]

    # A keeper is yours and off the board, whether or not the feed says so.
    # Dedupe: some platforms do record keepers as picks.
    # Applies in practice too: a rehearsal where the bots can draft your own
    # keepers isn't rehearsing your draft.
    keepers_rows = list(c.get("keepers") or [])
    # Keepers declared on the platform by ANY manager. Yours land on your
    # roster; everyone else's simply leave the board.
    board_by_key = {key(r["name"], r["position"]): r for r in rows}
    for k in league_keepers(name):
        row = board_by_key.get(key(k["name"], k["position"]))
        if row is None:
            continue
        if c.get("me") and str(k.get("owner_id")) == str(c["me"]):
            keepers_rows.append(row)
        else:
            taken.add(key(row["name"], row["position"]))
    have = {key(p["name"], p["position"]) for p in mine}
    for r in keepers_rows:
        k = key(r["name"], r["position"])
        taken.add(k)
        if k not in have:
            mine.append(r)
            have.add(k)

    on_clock = len(picks) + 1
    slot = sim["slot"] if sim else c["slot"]
    rounds = sim["rounds"] if sim else c["rounds"]
    my_picks = snake_picks_for_slot(slot, L.teams, rounds) if slot else []
    # Only for the real thing: a 3-round practice draft has no round 10 to lose.
    spent = set() if sim else set(c.get("keeper_rounds") or [])
    if spent and slot:
        my_picks = [p for p in my_picks
                    if ((p - 1) // L.teams) + 1 not in spent]
    upcoming = [p for p in my_picks if p >= on_clock]
    until = (upcoming[0] - on_clock) if upcoming else None
    gap = (upcoming[1] - upcoming[0]) if len(upcoming) > 1 else L.teams

    my_next = on_clock + (until or 0)
    recs = draft_mod.recommend(rows, L, taken, mine, limit=60,
                               next_pick=my_next, following_pick=my_next + gap)
    avail = [r for r in rows if key(r["name"], r["position"]) not in taken]
    # One live view of the remaining pool, shared by tiers, cost-of-waiting and
    # the value list, so nothing on the page is still quoting the preseason.
    taken_by_pos = collections.Counter(
        r["position"] for r in rows if key(r["name"], r["position"]) in taken)
    live_repl = vbd_mod.dynamic_replacement(avail, L, taken_by_pos)
    dyn = board_mod.live_ranks(avail, live_repl)
    value = sorted((r for r in avail
                    if dyn.get(key(r["name"], r["position"]), {}).get("dyn_edge")
                    is not None
                    and dyn[key(r["name"], r["position"])]["dyn_rank"] <= 170),
                   key=lambda r: -dyn[key(r["name"], r["position"])]["dyn_edge"])[:8]
    horizon = on_clock + (until or 0) + gap
    gone = sorted((r for r in avail
                   if (dyn.get(key(r["name"], r["position"]), {}).get("dyn_adp_rank")
                       or 10**6) + on_clock - 1 < horizon),
                  key=lambda r: dyn[key(r["name"], r["position"])]["dyn_adp_rank"])[:10]

    mine_raw = [p for p in picks if p.get("by") == c["me"]]
    late = special_pool(name, picks, mine_raw)
    tiers = {pos: tier_breaks([r for r in avail], pos, dyn)
             for pos in ("QB", "RB", "WR", "TE")}
    tier_of = {t["n"]: t for rows_ in tiers.values() for t in rows_}
    run = positional_run(picks)
    top3 = [{"n": r["name"], "p": r["pos_rank"],
             "why": why_mod.reasons(r, L, mine, next_pick=my_next,
                                    following_pick=my_next + gap,
                                    tier_of=tier_of, byes=BYES, run=run)}
            for r in recs[:3]]

    return {
        "league": name, "leagues": list(CTX), "teams": L.teams,
        "top3": top3,
        "manual": is_manual(name),
        "pool": [{"n": r["name"], "p": r["pos_rank"]} for r in avail[:320]],
        "on_clock": on_clock, "until": until, "slot": slot, "npicks": len(picks),
        # K and DST never enter `rows`, so roster_gaps can't see them filled or
        # empty. `late` already worked out which you still need -- use it, so
        # the roster shows every starting slot rather than quietly hiding two.
        "gaps": {**{s: n for s, n in draft_mod.roster_gaps(mine, L).items()
                    if s not in ("K", "DST")},
                 **{s: 1 for s in late.get("need", [])}},
        # Drafted kickers and defenses come off the pick records, not the
        # board, or your roster would silently omit them.
        "roster": [{"n": p.get("name"), "p": (p.get("position") or "").upper(),
                    "k": False}
                   for p in mine_raw
                   if (p.get("position") or "").upper() in DST_POS | {"K"}]
                  + [{"n": p["name"], "p": p["pos_rank"],
                    "k": key(p["name"], p["position"])
                         in {key(r["name"], r["position"]) for r in keepers_rows}}
                   for p in mine],
        "keeper_rounds": sorted(spent),
        "late": late,
        "rounds_left": max(0, rounds - ((on_clock - 1) // L.teams + 1)),
        "bridge": ({"age": round(time.time() - INGEST_TS[name])}
                   if c.get("ingest") and name in INGEST_TS
                   else ({"age": None} if c.get("ingest") else None)),
        "recs": [{"n": r["name"], "p": r["pos_rank"], "g": r["marginal"],
                  "v": r["vorp"],
                  "e": dyn.get(key(r["name"], r["position"]), {}).get("dyn_edge"),
                  "m": dyn.get(key(r["name"], r["position"]), {}).get("dyn_adp_rank"),
                  "s": r.get("rel_spread", 0), "gp": r.get("gone_pct"),
                  "dv": dyn.get(key(r["name"], r["position"]), {}).get("dyn_vorp"),
                  "pl": r.get("plan"), "bv": r.get("bench_val"),
                  "xs": r.get("exp_starts")} for r in recs],
        "value": [{"n": r["name"], "p": r["pos_rank"],
                   "e": dyn[key(r["name"], r["position"])]["dyn_edge"],
                   "m": dyn[key(r["name"], r["position"])]["dyn_adp_rank"]}
                  for r in value],
        "gone": [{"n": r["name"], "p": r["pos_rank"]} for r in gone],
        "last": [{"n": p["name"], "p": p.get("position"), "no": p["pick_no"]}
                 for p in picks[-6:]][::-1],
        "tiers": tiers,
        "wait": wait_cost(rows, taken, on_clock + (until or 0),
                          on_clock + (until or 0) + gap, dyn, on_clock),
        "run": run,
        "sim": sim_state(name),
        "ts": time.strftime("%H:%M:%S"),
    }


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>draft</title><style>
:root{--bg:#0f1115;--fg:#e8eaed;--dim:#8b93a1;--card:#171a21;--line:#252a34;
--go:#4ade80;--warn:#fbbf24;--bad:#f87171;--acc:#60a5fa}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;padding:12px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.tab{padding:6px 13px;border-radius:6px;background:var(--card);color:var(--dim);
border:1px solid var(--line);cursor:pointer;font-size:13px}
.tab.on{background:var(--acc);color:#0b1020;border-color:var(--acc);font-weight:700}
h1{font-size:19px;margin:0 0 2px;letter-spacing:.5px}
.sub{color:var(--dim);font-size:13px;margin-bottom:13px}
.live{color:var(--go);font-weight:700} .clock{color:var(--warn);font-weight:700}
.card{background:var(--card);border:1px solid var(--line);border-radius:9px;
padding:12px 14px;margin-bottom:11px}
.lbl{color:var(--dim);font-size:11px;letter-spacing:1.4px;text-transform:uppercase;
margin-bottom:8px}
table{width:100%;border-collapse:collapse}
td{padding:4px 6px;white-space:nowrap}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{display:inline-block;height:9px;border-radius:2px;background:var(--acc);
vertical-align:middle;margin-right:7px}
.pos{color:var(--dim);font-size:12px}
/* one size for every number in the board; colour carries the meaning, not size */
td.num{font-size:14px}
td.num.dim{color:var(--dim)}
td.num.acc{color:var(--acc);font-weight:700}
tr.hd td{color:var(--dim);font-size:10px;letter-spacing:1.2px;text-transform:uppercase;
border-bottom:1px solid var(--line);padding-bottom:6px}
.legend{color:var(--dim);font-size:12px;margin:-4px 0 9px;white-space:normal;
line-height:1.4}
.legend b{color:var(--fg);font-weight:600}
.pos.RB{color:#86efac}.pos.WR{color:#93c5fd}.pos.TE{color:#fca5a5}.pos.QB{color:#fcd34d}
.up{color:var(--go)}.dn{color:var(--bad)}.warnt{color:var(--warn)}
.clockme{background:#1b2410;border-color:#3d5220}
.clock2{background:#241c10;border-color:#513d18}
.simwait{background:#141a26;border-color:#2b3a52}
.simdone{background:#101f18;border-color:#1f4a37}
.big{font-size:34px;font-weight:700;letter-spacing:1px;line-height:1.15}
.big.hot{color:var(--bad)}
.cd{color:var(--warn);font-size:14px;margin-top:5px}.cd.hot{color:var(--bad);font-weight:700}
.rst{color:var(--acc);cursor:pointer;text-decoration:underline;font-size:13px;
margin-left:8px}
.need{display:inline-block;background:#2a1f14;color:var(--warn);border-radius:5px;
padding:2px 8px;margin-right:6px;font-size:13px}
.gone span,.roster span{display:inline-block;margin:0 10px 5px 0;color:var(--dim)}
.err{background:#2a1416;border-color:#4b1d22;color:#fca5a5}
.entry{background:#141a26;border-color:#2b3a52}
input#q{width:100%;padding:10px 12px;border-radius:7px;border:1px solid var(--line);
background:#0c0f14;color:var(--fg);font:15px ui-monospace,Menlo,monospace}
.hits{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}
.hit{padding:6px 11px;border-radius:6px;background:#1d2532;border:1px solid var(--line);
cursor:pointer;font-size:13px}
.hit:hover{background:#26304180}
.hit.sel{background:#2c3a52;border-color:var(--go)}
.hit b{color:var(--go)}
.mineflag{margin-top:9px;color:var(--dim);font-size:13px;cursor:pointer;user-select:none}
.mineflag input{margin-right:7px;transform:scale(1.2)}
.undo{float:right;color:var(--dim);cursor:pointer;font-size:12px;
text-decoration:underline}
.filters{display:flex;gap:5px;margin-bottom:9px;flex-wrap:wrap}
.f{padding:4px 12px;border-radius:14px;background:#1b2130;border:1px solid var(--line);
cursor:pointer;font-size:12px;color:var(--dim)}
.f.on{background:var(--acc);color:#0b1020;border-color:var(--acc);font-weight:700}
.scrollbox{height:252px;overflow-y:auto;overscroll-behavior:contain}
.scrollbox::-webkit-scrollbar{width:9px}
.scrollbox::-webkit-scrollbar-thumb{background:#2f3846;border-radius:5px}
.tierrow td{border-top:1px dashed #39445699}
.tiertag{color:var(--warn);font-size:10px;letter-spacing:.1em}
.run{background:#2a2113;border-color:#4a3a18;color:var(--warn)}
.case{background:#101828;border-color:#24354f}
.case ol{margin:0;padding-left:22px}
.case li{margin-bottom:9px;line-height:1.45}
.case li::marker{color:var(--acc);font-weight:700}
.case .nm{font-weight:700}
.case .rz{color:var(--dim);display:block;font-size:13px;margin-top:1px;
white-space:normal}
.late{background:#221a10;border-color:#4a3a18}
.late.urgent{background:#2a1416;border-color:#5b2027}
.late td{padding:3px 6px}
.late .opp{color:var(--dim);font-size:12px}
.wait table{font-size:13px}
.wait .cost{color:var(--bad);font-weight:700}
.wait .cheap{color:var(--go)}
@media(max-width:640px){body{padding:8px}td{padding:3px 4px}}
</style></head><body>
<div id=tabs class=tabs></div><div id=app>loading…</div>
<script>
let LEAGUE=new URLSearchParams(location.search).get('league')||'';
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let HITS=[],SEL=0;
// Mirrors ff.names.normalize on the client: strip accents and punctuation so
// the query and the candidate compare on letters alone.
const nk=s=>String(s??'').toLowerCase().normalize('NFD')
  .replace(/[\\u0300-\\u036f]/g,'').replace(/[^a-z ]/g,'').replace(/\\s+/g,' ').trim();
const pos=p=>`<span class="pos ${(p||'').replace(/[0-9]/g,'')}">${esc(p)}</span>`;
const sg=v=>v==null?'':`<span class="${v>0?'up':v<0?'dn':''}">${v>0?'+':''}${v}</span>`;
// odds he's taken before you pick again -- the reason the order isn't just by value
const gone=v=>v==null?'<span class=pos>—</span>'
 :`<span class="${v>=70?'dn':v>=35?'warnt':'pos'}">${v}%</span>`;
const fmt=s=>s==null?'—':`${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;
function tabs(d){
 document.getElementById('tabs').innerHTML=d.leagues.map(n=>
  `<div class="tab ${n===d.league?'on':''}" onclick="pick('${esc(n)}')">${esc(n)}</div>`).join('');
}
function pick(n){
 LEAGUE=n;window._last=null;          // don't tick the old league's clock here
 history.replaceState({},'',`?league=${encodeURIComponent(n)}`);
 document.getElementById('tabs').innerHTML=
  Array.from(document.querySelectorAll('#tabs .tab')).map(t=>
   `<div class="tab ${t.textContent===n?'on':''}" `
   +`onclick="pick('${esc(t.textContent)}')">${esc(t.textContent)}</div>`).join('');
 tick();
}
function render(d){
 // The bar has to be drawn from the SAME number the rows are sorted by, or it
 // reads as a broken list. `pl` (value now + expected value of your next pick)
 // is the sort key whenever we know your pick numbers; `g` otherwise.
 // Marginal value and plan are 0 for everyone from ~round 7, so the bar has
 // to fall through to season value or it goes uniformly blank for half the draft.
 const sv=r=>{const a=r.pl!=null?r.pl:r.g; return a!==0?a:(r.bv??0);};
 // Scale across the real range, not from zero: the sort key sits in a narrow
 // band well above zero, so a zero-based bar is visually flat and says nothing.
 const mn=Math.min(...d.recs.map(sv));
 const mx=Math.max(1,...d.recs.map(r=>sv(r)-mn));
 // Built into its own string, not appended to `h` -- `h` is declared further
 // down, and touching it up here throws a temporal-dead-zone ReferenceError
 // that tick() swallows, leaving the previous league on screen.
 const S=d.sim;
 let simcard='';
 if(S){
  if(S.done){
   simcard=`<div class="card simdone"><b>practice draft complete</b> — ${S.rounds} rounds`
    +(S.autopicked.length?` · autopicked for you: ${S.autopicked.map(esc).join(', ')}`:'')
    +(S.transcribe?` · typed ${S.typed}, missed ${S.missed.length}`:'')
    +` <span class=rst onclick="resetSim()">run it again</span></div>`;
  } else if(S.pending){
   simcard=`<div class="card clock2"><div class=lbl>team ${S.pending.slot} is on the clock`
    +` — type this pick in</div><div class=big>${esc(S.pending.name)}`
    +` <span class=pos>${esc(S.pending.position)}</span></div>`
    +`<div class="cd ${S.left<=5?'hot':''}">${S.left==null?'':S.left+'s to enter it'}</div></div>`;
  } else if(S.yours){
   simcard=`<div class="card clockme"><div class=lbl>you are on the clock`
    +` — round ${S.round}, pick ${S.pick_no} of ${S.total}</div>`
    +`<div class="big ${S.left<=10?'hot':''}">${fmt(S.left)}</div>`
    +`<div class=pos>pick from the board below, or it autopicks for you</div></div>`;
  } else {
   simcard=`<div class="card simwait"><div class=lbl>practice draft — round ${S.round}`
    +` of ${S.rounds}, pick ${S.pick_no} of ${S.total}</div>`
    +`<div>team ${S.on_slot} on the clock… <span class=pos>(you are slot ${S.slot})</span>`
    +` <span class=rst onclick="resetSim()">restart</span></div></div>`;
  }
  if(d.reject) simcard+=`<div class="card err">${esc(d.reject)}</div>`;
 }
 let entry='';
 // A bridged league gets its picks from the ESPN draft room every 2s, and each
 // post REPLACES the pick list. Typing here while that is live would be wiped
 // within two ticks, so the entry box only appears once the bridge goes quiet
 // -- at which point it is the fallback, and says so.
 const br = d.bridge;
 const bridgeLive = br && br.age != null && br.age < 12;
 if(br){
  entry = bridgeLive
   ? `<div class="card entry"><div class=lbl>live from the espn draft room</div>`
     +`<div>picks are arriving automatically `
     +`<span class=pos>· last sync ${br.age}s ago</span></div></div>`
   : `<div class="card err"><div class=lbl>bridge not connected</div>`
     +`<div>No picks received${br.age==null?' yet':` for ${br.age}s`}. `
     +`Open the ESPN draft room with the userscript active, or enter picks by hand below.</div></div>`;
 }
 if(d.manual && !bridgeLive){
  entry+=`<div class="card entry"><div class=lbl>manual entry — no live feed for this league`
   +`<span class=undo onclick="undoPick()">undo last</span></div>`
   +`<input id=q placeholder="type a name, then click to mark drafted…" `
   +`autocomplete=off oninput="SEL=0;hits()" onkeydown="navHits(event)">`
   +`<div class=hits id=hits></div>`
   +`<label class=mineflag><input type=checkbox id=mine> this pick is MINE</label></div>`;
 }
 let h=`<h1>${esc(d.league)} · pick ${d.on_clock}</h1><div class=sub>`;
 h+= d.until===0?`<span class=live>● YOU ARE ON THE CLOCK</span>`
   : d.until==null?`draft not started / slot unknown`
   : `you're up in <span class=clock>${d.until}</span> pick${d.until==1?'':'s'}`;
 h+=` · slot ${d.slot??'?'} · ${d.npicks} picks in · ${esc(d.ts)}</div>`;
 h+=simcard;
 if(d.run) h+=`<div class="card run"><b>${d.run.pos} run</b> — ${d.run.n} of the last `
   +`${d.run.of} picks. Get ahead of it or wait it out deliberately.</div>`;
 h+=entry;
 h+=`<div class=card><div class=lbl>your roster (${d.roster.length})</div><div class=roster>`;
 h+= d.roster.length?d.roster.map(r=>`<span>${esc(r.n)} ${pos(r.p)}`
   +`${r.k?' <span class=tiertag>KEPT</span>':''}</span>`).join(''):'<span>—</span>';
 if(d.keeper_rounds&&d.keeper_rounds.length)
  h+=`<div class=pos style="margin-top:6px">no pick in round `
   +`${d.keeper_rounds.join(', ')} — spent on keepers</div>`;
 h+=`</div><div style="margin-top:9px">`;
 const g=Object.entries(d.gaps);
 h+= g.length?g.map(([s,n])=>`<span class=need>${esc(s)} ×${n}</span>`).join('')
    :`<span class=need style="background:#12240f;color:var(--go)">starters full</span>`;
 h+=`</div></div>`;

 if(d.top3&&d.top3.length){
  h+=`<div class="card case"><div class=lbl>the case for your next pick</div><ol>`;
  for(const t of d.top3)
   h+=`<li><span class=nm>${esc(t.n)}</span> ${pos(t.p)}`
    +`<span class=rz>${esc(t.why)}</span></li>`;
  h+=`</ol></div>`;
 }

 // Two starting slots live off the main board on purpose. You won't draft
 // either until the end, so this stays out of the way until it matters, then
 // moves to the top. The threshold is how many slots you still need: with two
 // holes and two rounds left, every remaining pick is spoken for.
 let lateCard='', lateLine='';
 if(d.late && d.late.need.length){
  const need=d.late.need, left=d.rounds_left;
  const promote = left!=null && left <= need.length;
  const urgent  = left!=null && left <  need.length;
  const hdr=`still need ${need.join(' + ')}`
    +(left!=null?` · ${left} round${left==1?'':'s'} left`:'');
  if(promote){
   lateCard=`<div class="card late${urgent?' urgent':''}"><div class=lbl>${hdr}</div>`;
   if(d.late.dst.length){
    lateCard+=`<div class=pos style="margin-bottom:4px">best defenses by projected`
     +` points, weeks 1-3 — a streaming slot, not a season</div><table>`;
    for(const x of d.late.dst)
     lateCard+=`<tr><td>${esc(x.name)}</td><td class=num>${x.pts}</td>`
      +`<td class=opp>${esc(x.opps.join(', '))}</td></tr>`;
    lateCard+=`</table>`;
   }
   if(d.late.k.length){
    lateCard+=`<div class=pos style="margin:6px 0 4px">kickers</div><table>`;
    for(const x of d.late.k)
     lateCard+=`<tr><td>${esc(x.n||x.name)} <span class=pos>${esc(x.team||'')}</span></td>`
      +`<td class=num>${x.pts==null?'—':x.pts}</td></tr>`;
    lateCard+=`</table>`;
   }
   lateCard+=`</div>`;
  } else {
   // Just a footnote until then, so you can't forget they exist.
   lateLine=`<div class="card late"><div class=lbl>${hdr}</div>`
    +`<div class=pos>the shortlist appears here when you're `
    +`${need.length} round${need.length==1?'':'s'} out</div></div>`;
  }
 }
 h+=lateCard;

 const ordered=d.recs.some(r=>r.pl!=null);
 h+=`<div class=card><div class=lbl>take now</div>`
  +(FILTER==='DST'
    ? `<div class=legend><b>projected points over the first three weeks</b>, with`
      +` the matchups — a defense is a streaming slot, so you're drafting a`
      +` favourable September, not a season. Source's own scoring, not your`
      +` league's.</div>`
    : FILTER==='K'
    ? `<div class=legend><b>season projection</b>, scored under your league's`
      +` rules where it publishes any. Take one in the last round.</div>`
    : `<div class=legend>ranked by <b>what this pick is worth to your lineup</b>`
  +`, then by <b>expected season points</b> (starts x value over a streamer)`
  +` once your starters are full`
  +(ordered?` <b>plus what you'd still get at your next pick</b> — so a player`
    +` who won't last can outrank one worth slightly more who will`:'')
  +`</div>`)
  +`<div class=filters>`;
 // DST and K get tabs too, so you can look at them before the last round --
 // same numbers as the reminder card, just on demand.
 const tabsFor=['ALL','QB','RB','WR','TE'].concat((d.late&&d.late.slots)||[]);
 for(const f of tabsFor)
  h+=`<div class="f ${FILTER===f?'on':''}" onclick="setf('${f}')">${f}</div>`;
 h+=`</div><div class=scrollbox><table>`;
 // The player columns mean nothing on the DST/K tabs, which bring their own.
 if(FILTER!=='DST'&&FILTER!=='K')
  h+=`<tr class=hd><td>player</td><td class=num>lineup</td>`
   +`<td class=num title="expected season points: starts x value over a streamer">season</td>`
   +`<td class=num>${ordered?'gone by next pick':''}</td>`
   +`<td class=num>adp</td><td class=num>edge</td></tr>`;
 if(FILTER==='DST'||FILTER==='K'){
  const rows=(FILTER==='DST'?(d.late&&d.late.dst):(d.late&&d.late.k))||[];
  h+=FILTER==='DST'
    ? `<tr class=hd><td>defense</td><td class=num>wks 1-3</td><td>opponents</td></tr>`
    : `<tr class=hd><td>kicker</td><td class=num>proj</td><td></td></tr>`;
  for(const x of rows)
   h+= FILTER==='DST'
    ? `<tr><td>${esc(x.name)}</td><td class="num acc">${x.pts}</td>`
      +`<td class=opp>${esc(x.opps.join(', '))}</td></tr>`
    : `<tr><td>${esc(x.name)} <span class=pos>${esc(x.team||'')}</span></td>`
      +`<td class="num acc">${x.pts==null?'—':x.pts}</td><td></td></tr>`;
  if(!rows.length) h+=`<tr><td class=pos>nothing left at ${FILTER}</td></tr>`;
  h+=`</table></div></div>`;
 } else {
 const shown=d.recs.filter(r=>FILTER==='ALL'||r.p.replace(/[0-9]/g,'')===FILTER);
 let prevTier=null;
 for(const r of shown){
  const bare=r.p.replace(/[0-9]/g,'');
  const tl=(d.tiers||{})[bare]||[];
  const te=tl.find(x=>x.n===r.n);
  const brk=(FILTER!=='ALL'&&te&&prevTier!==null&&te.tier!==prevTier);
  if(te) prevTier=te.tier;
  h+=`<tr class="${brk?'tierrow':''}"><td style="width:50%">`
   +`<span class=bar style="width:${Math.round((sv(r)-mn)/mx*70)}px"></span>`
   +`${esc(r.n)} ${pos(r.p)}${r.s>0.2?' <span title="sources disagree" style="color:var(--warn)">◆</span>':''}`
   +`${brk?' <span class=tiertag>TIER '+te.tier+'</span>':''}</td>`
   +`<td class="num acc">+${Math.round(r.g)}</td>`
   +`<td class="num dim" title="expected starts: ${r.xs??'—'}">`
   +`${r.bv==null?'—':Math.round(r.bv)}</td>`
   +`<td class=num>${gone(r.gp)}</td>`
   +`<td class="num dim">${r.m??'—'}</td><td class=num>${sg(r.e)}</td></tr>`;}
 if(!shown.length) h+=`<tr><td class=pos>nothing left at ${FILTER}</td></tr>`;
 h+=`</table></div></div>`;
 }
 if(d.wait&&Object.keys(d.wait).length){
  h+=`<div class="card wait"><div class=lbl>cost of waiting — best now vs. your next pick</div><table>`;
  for(const p in d.wait){const w=d.wait[p];
   const c=(w.cost==null)?'—':(w.cost>12?`<span class=cost>-${w.cost}</span>`
        :`<span class=cheap>-${w.cost}</span>`);
   h+=`<tr><td>${pos(p)}</td><td>${esc(w.now)} <span class=pos>(${w.now_v})</span></td>`
    +`<td class=pos>&rarr; ${w.later?esc(w.later)+' ('+w.later_v+')':'nobody survives'}</td>`
    +`<td class=num>${c}</td></tr>`;}
  h+=`</table></div>`;}

 h+=`<div class=card><div class=lbl>value — falling past their price</div><table>`;
 for(const r of d.value)
  h+=`<tr><td>${esc(r.n)} ${pos(r.p)}</td><td class=num>mkt ${r.m}</td><td class=num>${sg(r.e)}</td></tr>`;
 h+=`</table></div><div class=card><div class=lbl>gone before your next pick</div><div class=gone>`;
 h+= d.gone.length?d.gone.map(r=>`<span>${esc(r.n)} ${pos(r.p)}</span>`).join(''):'<span>—</span>';
 h+=`</div></div><div class=card><div class=lbl>last picks</div><div class=gone>`;
 h+= d.last.map(r=>`<span>${r.no}. ${esc(r.n)} ${pos(r.p)}</span>`).join('')||'<span>—</span>';
 return h+`</div></div>`+lateLine;
}
let POOL=[];let FILTER='ALL';
function setf(p){FILTER=p;const d=window._last;if(d)document.getElementById('app').innerHTML=render(d);}
function hits(){
 const q=(document.getElementById('q').value||'').toLowerCase().trim();
 const box=document.getElementById('hits');
 if(!q){box.innerHTML='';HITS=[];return;}
 // Punctuation-blind: "jamarr" has to find "Ja'Marr Chase". Under a 30s clock
 // you cannot be made to type an apostrophe correctly.
 const nq=nk(q);
 const scored=[];
 for(const p of POOL){
  const n=nk(p.n), last=n.split(' ').slice(-1)[0];
  let r=-1;
  if(n.startsWith(nq))       r=0;   // full-name prefix
  else if(last.startsWith(nq)) r=1; // last-name prefix -- how you actually type
  else if(n.includes(nq))    r=2;   // anywhere
  if(r>=0) scored.push([r,p]);
 }
 scored.sort((a,b)=>a[0]-b[0]);
 HITS=scored.slice(0,8).map(x=>x[1]);
 if(SEL>=HITS.length) SEL=0;
 // Index into HITS rather than interpolating the name into an onclick string:
 // any name containing a quote used to emit broken JS and the click died.
 box.innerHTML=HITS.map((p,i)=>`<div class="hit${i===SEL?' sel':''}" onclick="take(${i})">`
   +`${esc(p.n)} ${pos(p.p)}</div>`).join('')||'<span style="color:var(--dim)">no match</span>';
}
function navHits(e){
 if(e.key==='ArrowDown'||e.key==='ArrowUp'){
  e.preventDefault();
  if(!HITS.length) return;
  SEL=(SEL+(e.key==='ArrowDown'?1:HITS.length-1))%HITS.length;
  hits();
 } else if(e.key==='Enter'){ e.preventDefault(); first(); }
}
function first(){
 if(HITS.length) take(SEL);
}
async function take(i){
 const p=HITS[i]; if(!p) return;
 const n=p.n;
 const mine=document.getElementById('mine').checked?1:0;
 document.getElementById('q').value='';document.getElementById('hits').innerHTML='';
 HITS=[];SEL=0;
 const d=await (await fetch(`/pick?league=${encodeURIComponent(LEAGUE)}`
   +`&name=${encodeURIComponent(n)}&mine=${mine}`)).json();
 if(!d.error){POOL=d.pool||[];window._last=d;tabs(d);
   document.getElementById('app').innerHTML=render(d);}
 const q=document.getElementById('q'); if(q) q.focus();
}
async function undoPick(){
 const d=await (await fetch(`/undo?league=${encodeURIComponent(LEAGUE)}`)).json();
 if(!d.error){POOL=d.pool||[];window._last=d;tabs(d);
   document.getElementById('app').innerHTML=render(d);}
}
async function resetSim(){
 const d=await (await fetch(`/sim/reset?league=${encodeURIComponent(LEAGUE)}`)).json();
 if(!d.error){POOL=d.pool||[];window._last=d;tabs(d);
   document.getElementById('app').innerHTML=render(d);}
}
// The clock ticks locally between polls so it counts down smoothly; every poll
// re-syncs it to the server, which is the authority on when time is up.
setInterval(()=>{
 const d=window._last; if(!d||!d.sim||d.sim.left==null||d.sim.done) return;
 d.sim.left=Math.max(0,d.sim.left-1);
 const el=document.querySelector('.big,.cd'); if(!el) return;
 if(d.sim.pending){el.textContent=d.sim.left+'s to enter it';
   el.className='cd'+(d.sim.left<=5?' hot':'');}
 else if(d.sim.yours){el.textContent=fmt(d.sim.left);
   el.className='big'+(d.sim.left<=10?' hot':'');}
},1000);
let SEQ=0;
async function tick(){
 clearTimeout(window._t);          // cancel any pending run before starting
 const my=++SEQ;
 try{
  const d=await (await fetch('/state?league='+encodeURIComponent(LEAGUE))).json();
  // A slower response for the league you just switched AWAY from must not
  // overwrite the one you asked for -- it would snap the board back.
  if(my!==SEQ) return;
  if(d.error){document.getElementById('app').innerHTML=
     `<div class="card err">${esc(d.error)}</div>`;}
  else{
    LEAGUE=d.league;POOL=d.pool||[];window._last=d;tabs(d);
    const q=document.getElementById('q');
    // The poll rebuilds this input under you. Carry the caret and selection
    // across too, not just the text -- without it, typing fast against a clock
    // loses your cursor every refresh.
    const keep=q?{v:q.value,f:document.activeElement===q,
                  s:q.selectionStart,e:q.selectionEnd}:null;
    // Carry scroll across the re-render. Without this a refresh snaps the
    // board back to the top, which at a one-second cadence means you can
    // never scroll it at all.
    const sb=document.querySelector('.scrollbox');
    const sTop=sb?sb.scrollTop:0;
    document.getElementById('app').innerHTML=render(d);
    const sb2=document.querySelector('.scrollbox');
    if(sb2&&sTop) sb2.scrollTop=sTop;
    if(keep){const q2=document.getElementById('q');
             if(q2){q2.value=keep.v;
                    if(keep.f){q2.focus();
                      try{q2.setSelectionRange(keep.s,keep.e);}catch(_){}
                      hits();}}}
  }
 }catch(e){
  // Never fail silently: a render bug used to leave the last league frozen on
  // screen with no clue why.
  console.error('tick',e);
  document.getElementById('app').innerHTML=
    `<div class="card err">render failed: ${esc(e&&e.message||e)}</div>`;
 }
 if(my!==SEQ) return;              // a newer tick owns the schedule now
 // A practice draft moves on a clock, so it needs a much tighter poll than a
 // draft board that is just watching a feed.
 const L=window._last;
 const fast=L&&L.sim&&!L.sim.done;
 const near=L&&L.until!=null&&L.until<=3;   // your pick is imminent
 clearTimeout(window._t);
 window._t=setTimeout(tick,fast?1000:(near?750:1000));
}
tick();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        # The scraper posts from fantasy.espn.com, so this is cross-origin. The
        # server is bound to localhost and holds nothing secret, so a blanket
        # allow is fine and saves fighting preflight on draft day.
        self.send_header("Access-Control-Allow-Origin", "*")
        # Chrome's Private Network Access: a page on a public origin
        # (fantasy.espn.com) reaching a private one (localhost) is blocked
        # unless the local server opts in. Without this the scraper's requests
        # just hang -- no CORS error, no refusal, they simply never resolve.
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_POST(self):
        if not self.path.startswith("/ingest"):
            self.send_response(404)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = (q.get("league") or [""])[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode() or "{}")
            with LOCK:
                if name not in CTX:
                    raise KeyError(f"no league {name!r}")
                picks, unmatched = ingest_picks(name, payload.get("picks") or [])
                body = json.dumps({"ok": True, "picks": len(picks),
                                   "unmatched": unmatched[:10],
                                   "on_clock": len(picks) + 1}).encode()
        except Exception as e:
            body = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
        self._json(body)

    def do_GET(self):
        if self.path.startswith("/sim/reset"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = (q.get("league") or [""])[0]
            try:
                with LOCK:
                    s = SIM[name]
                    sim_start(name, s["slot"], s["rounds"], s["clock"],
                              s["bot_secs"], s["transcribe"], s["entry_secs"])
                    body = json.dumps(state_for(name)).encode()
            except Exception as e:
                body = json.dumps({"error": f"{type(e).__name__}: {e}",
                                   "leagues": list(CTX), "league": name}).encode()
            self._json(body)
            return
        if self.path.startswith("/pick") or self.path.startswith("/undo"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = (q.get("league") or [""])[0]
            try:
                with LOCK:
                    if self.path.startswith("/undo"):
                        undo_manual_pick(name)
                    else:
                        who = (q.get("name") or [""])[0]
                        mine = (q.get("mine") or ["0"])[0] == "1"
                        row = next((r for r in CTX[name]["rows"]
                                    if r["name"] == who), None)
                        if row:
                            ok, err = sim_accept(name, row)
                            if not ok:
                                body = json.dumps(
                                    {**state_for(name), "reject": err}).encode()
                                self._json(body)
                                return
                            add_manual_pick(name, row, sim_owns(name, mine))
                    body = json.dumps(state_for(name)).encode()
            except Exception as e:
                body = json.dumps({"error": f"{type(e).__name__}: {e}",
                                   "leagues": list(CTX), "league": name}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/state"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = (q.get("league") or [""])[0] or next(iter(CTX))
            if name not in CTX:
                name = next(iter(CTX))
            try:
                with LOCK:
                    body = json.dumps(state_for(name)).encode()
            except Exception as e:
                body = json.dumps({"error": f"{type(e).__name__}: {e}",
                                   "leagues": list(CTX), "league": name}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--sim", metavar="LEAGUE",
                    help="run a practice draft for this league instead of a live one")
    ap.add_argument("--slot", type=int, default=5, help="your draft slot (sim)")
    ap.add_argument("--rounds", type=int, default=3, help="rounds to simulate")
    ap.add_argument("--clock", type=int, default=90,
                    help="seconds on your clock (sim)")
    ap.add_argument("--bot-secs", type=float, default=6.0,
                    help="seconds each bot takes to pick (sim)")
    ap.add_argument("--transcribe", action="store_true",
                    help="drill the real draft-day job: type in every pick the "
                         "room makes, against a clock")
    ap.add_argument("--entry-secs", type=int, default=20,
                    help="seconds to type in an opponent's pick (--transcribe)")
    args = ap.parse_args()

    cfgs = {c["name"]: c for c in
            yaml.safe_load((ROOT / "leagues" / "leagues.yaml").read_text())["leagues"]}
    leagues, errors = load_all()
    print("preparing boards…")
    prepare(leagues, cfgs, fetch())
    for nm, err in errors:
        print(f"  skip {nm}: {err[:60]}")

    if args.sim:
        if args.sim not in CTX:
            print(f"\n  no league '{args.sim}'. have: {', '.join(CTX)}")
            return
        if not 1 <= args.slot <= CTX[args.sim]["league"].teams:
            print(f"\n  --slot must be 1..{CTX[args.sim]['league'].teams}")
            return
        sim_start(args.sim, args.slot, args.rounds, args.clock, args.bot_secs,
                  args.transcribe, args.entry_secs)
        threading.Thread(target=sim_loop, daemon=True).start()
        print(f"\n  PRACTICE DRAFT — {args.sim}: {args.rounds} rounds, "
              f"you are slot {args.slot} of {CTX[args.sim]['league'].teams}, "
              f"{args.clock}s clock"
              + (f", transcribe drill ({args.entry_secs}s per pick)"
                 if args.transcribe else ""))
        print("  picks go to data/manual_picks/_sim_*.json — your real draft "
              "file is untouched")

    ip = "localhost"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    print(f"\n  ->  http://localhost:{args.port}")
    print(f"  ->  http://{ip}:{args.port}   (phone / tablet on same wifi)")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
