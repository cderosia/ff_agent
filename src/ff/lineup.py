"""Weekly projections, availability, and lineup optimisation.

Season totals are the right input for a draft and the wrong one for a Sunday.
In-season we use each provider's WEEKLY projection, which already carries the
things you'd otherwise try to model yourself -- opponent, depth chart, and
injury news -- refreshed continuously by the providers.

What the providers do NOT reliably do is stop you starting someone who is out
or on bye. That is where real points are lost, and it is fully automatable, so
it happens here.
"""
from __future__ import annotations

import json
import pathlib
import time

import pandas as pd
import requests

from .names import key
from .stats import ESPN_STAT, SLEEPER_STAT, SLOT_ELIGIBILITY, score

ROOT = pathlib.Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "raw"
SEASON = 2026
UA = {"User-Agent": "Mozilla/5.0"}
ESPN_URL = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{SEASON}"
            f"/segments/0/leaguedefaults/3?view=kona_player_info")

OUT_STATUSES = {"Out", "IR", "PUP", "Suspended", "NA", "Doubtful", "DNR"}
RISK_STATUSES = {"Questionable"}


# ---------------------------------------------------------------------------
# weekly projections
# ---------------------------------------------------------------------------
def espn_weekly(week: int, limit: int = 600, max_age_hours: int = 1,
                force: bool = False) -> dict:
    """{(name,pos): stat_line} from ESPN's week-specific projection."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"espn_proj_{SEASON}_raw.json"
    if path.exists() and not force and \
            (time.time() - path.stat().st_mtime) / 3600 < max_age_hours:
        payload = json.loads(path.read_text())
    else:
        hdrs = dict(UA)
        hdrs["x-fantasy-filter"] = json.dumps({"players": {
            "limit": limit,
            "sortDraftRanks": {"sortPriority": 1, "sortAsc": True, "value": "PPR"}}})
        r = requests.get(ESPN_URL, headers=hdrs, timeout=60)
        r.raise_for_status()
        payload = r.json()
        path.write_text(json.dumps(payload))

    POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE"}
    out = {}
    for e in payload.get("players", []):
        p = e["player"]
        pos = POS.get(p.get("defaultPositionId"))
        if not pos:
            continue
        # ESPN can return MORE THAN ONE row for the same week: a real projection
        # and an empty stub with appliedTotal 0 and no stats. `next()` took
        # whichever happened to come first, so when the stub sorted first the
        # stat line came out empty, `if line` dropped the player, and he
        # silently showed 0.0 -- intermittently, because the order is not
        # stable between pulls. Caught on Brock Bowers (a 15.3 row and a 0.0
        # stub for the same week 1) after it had already hit TreVeyon Henderson.
        # Take the richest row, never merely the first.
        cands = [x for x in p.get("stats", [])
                 if x.get("seasonId") == SEASON and x.get("statSourceId") == 1
                 and x.get("statSplitTypeId") == 1
                 and x.get("scoringPeriodId") == week]
        if not cands:
            continue
        s = max(cands, key=lambda x: len(x.get("stats") or {}))
        line = {}
        for sid, val in (s.get("stats") or {}).items():
            nm = ESPN_STAT.get(int(sid))
            if nm and val:
                line[nm] = float(val)
        if line:
            out[key(p["fullName"], pos)] = line
    return out


def espn_injuries(max_age_hours: int = 1) -> dict:
    """{(name,pos): ESPN injuryStatus} -- a second opinion on availability.

    The Sleeper player blob is the only injury source `availability()` reads,
    and it goes stale: on the morning of week 1 it called Brock Bowers ACTIVE
    while ESPN had him DOUBTFUL and both projection feeds had dropped him. Two
    disagreeing sources is worth more than one confident one, so the worse of
    the two wins -- benching a healthy player costs you his points, starting an
    inactive one costs you the whole slot.
    """
    path = CACHE / f"espn_proj_{SEASON}_raw.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {}
    POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}
    out = {}
    for e in payload.get("players", []):
        p = e["player"]
        pos = POS.get(p.get("defaultPositionId"))
        st = p.get("injuryStatus")
        if pos and st and st not in ("ACTIVE", "NORMAL"):
            out[key(p.get("fullName", ""), pos)] = st.replace("_", " ").title()
    return out


def sleeper_weekly(week: int, max_age_hours: int = 1, force: bool = False) -> dict:
    """{(name,pos): stat_line} from Sleeper/Rotowire's week projection.

    Carries KICKERS as well as the skill positions, and deliberately does so
    alone -- espn_weekly leaves them out even though ESPN's payload has them.
    The reason is scoring basis, not availability: ESPN reports made field goals
    only as BUCKETS (0-39 / 40-49 / 50+), while 719 scores kickers by DISTANCE
    (`fg_yds`). An ESPN line therefore scores near zero from field goals in that
    league, and blending it against a Sleeper line that scores correctly would
    average a right answer with a wrong one. Sleeper's line carries the buckets
    AND `fgm_yds`, so it scores properly under either league's rules.

    Defenses are still absent here -- they have no comparable stat line -- and
    come from `special.dst_week()` instead.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    # Renamed when kickers were added: the old file holds a K-less payload, and
    # silently reusing it would leave every kicker projected at zero.
    path = CACHE / f"sleeper_wk_{SEASON}_w{week}.json"
    if path.exists() and not force and \
            (time.time() - path.stat().st_mtime) / 3600 < max_age_hours:
        rows = json.loads(path.read_text())
    else:
        url = (f"https://api.sleeper.com/projections/nfl/{SEASON}/{week}"
               f"?season_type=regular&position[]=QB&position[]=RB"
               f"&position[]=WR&position[]=TE&position[]=K&order_by=pts_ppr")
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        rows = r.json()
        path.write_text(json.dumps(rows))

    out = {}
    for row in rows:
        pl = row.get("player") or {}
        pos = pl.get("position")
        if pos not in ("QB", "RB", "WR", "TE", "K"):
            continue
        line = {}
        for k, v in (row.get("stats") or {}).items():
            nm = SLEEPER_STAT.get(k)
            if nm and v:
                line[nm] = float(v)
        if line:
            nm_full = f"{pl.get('first_name','')} {pl.get('last_name','')}".strip()
            out[key(nm_full, pos)] = line
    return out


def weekly_points(week: int, scoring: dict) -> dict:
    """{(name,pos): (points, n_sources, spread)} blended for one week."""
    a, b = espn_weekly(week), sleeper_weekly(week)
    out = {}
    for k in set(a) | set(b):
        lines = [x for x in (a.get(k), b.get(k)) if x]
        pts = [score(line, scoring) for line in lines]
        out[k] = (sum(pts) / len(pts), len(pts),
                  (max(pts) - min(pts)) if len(pts) > 1 else 0.0)
    return out


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------
def schedule(season: int = SEASON) -> pd.DataFrame:
    path = CACHE / "nflverse_games.csv"
    if not path.exists():
        r = requests.get(
            "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv",
            timeout=120)
        r.raise_for_status()
        path.write_bytes(r.content)
    d = pd.read_csv(path, low_memory=False)
    return d[d.season == season]


def bye_teams(week: int, season: int = SEASON) -> set:
    """Teams NOT playing in a given week."""
    s = schedule(season)
    playing = set(s[s.week == week].home_team) | set(s[s.week == week].away_team)
    allteams = set(s.home_team) | set(s.away_team)
    return allteams - playing


def opponents(week: int, season: int = SEASON) -> dict:
    """{team: "@KC" / "vs KC"} for one week. Teams on bye are absent.

    Uses nflverse team codes, which disagree with the projection feeds'
    (LA/JAX vs LAR/JAC) -- callers should resolve through starts.TEAM_ALIASES
    the same way byes are, or a handful of players silently show no opponent.
    """
    s = schedule(season)
    out = {}
    for _, g in s[s.week == week].iterrows():
        out[g.home_team] = f"vs {g.away_team}"
        out[g.away_team] = f"@ {g.home_team}"
    return out



_BLOB_TEAM: dict = {}


def _team_from_blob(blob: dict, name: str, pos: str) -> str | None:
    """Pro team for a player the roster feed gave us no team for."""
    from .names import key as nkey
    if not _BLOB_TEAM:
        for pid, p in (blob or {}).items():
            nm = p.get("full_name") or " ".join(
                x for x in (p.get("first_name"), p.get("last_name")) if x)
            po = p.get("position")
            if nm and po and p.get("team"):
                _BLOB_TEAM[nkey(nm, "DST" if po == "DEF" else po)] = p["team"]
            # Sleeper keys team defenses by the team code itself.
            if po == "DEF" and pid.isalpha():
                _BLOB_TEAM[("__dst__", pid.upper())] = pid.upper()
    hit = _BLOB_TEAM.get(nkey(name, pos))
    if hit:
        return hit
    if pos == "DST":
        # "Eagles D/ST", "Baltimore Ravens" -- match on any word that is a
        # known nickname or city fragment of a team we have.
        for k, v in _BLOB_TEAM.items():
            if k[0] == "__dst__" and v.lower() in name.lower():
                return v
        import re
        words = [w for w in re.split(r"[^A-Za-z]+", name) if len(w) > 2]
        for pid, p in (blob or {}).items():
            if p.get("position") == "DEF" and p.get("team"):
                label = f"{p.get('first_name','')} {p.get('last_name','')}".lower()
                if any(w.lower() in label for w in words):
                    return p["team"]
    return None


def build_pool(league, players, week: int, by_key: dict | None = None,
               blob: dict | None = None) -> list[dict]:
    """Roster rows ready to optimise, INCLUDING kickers and defenses.

    Built from the raw platform roster rather than from board rows. Board rows
    deliberately omit K and DST (see ff.special), so anything built off them
    leaves two starting slots empty and runs a kicker and a defense light --
    which is exactly what the Lineup tab did until this existed.

    Defenses have no weekly stat line anywhere and are priced from
    `special.dst_week`; kickers ride in the weekly blend. Same construction
    winprob.project_team uses, so a lineup's total and the projection behind its
    win probability agree instead of differing by ~15 points.
    """
    from .names import key as nkey
    from .special import dst_week

    by_key = by_key or {}
    wp = weekly_points(week, league.scoring)
    dsts = dst_week(week)
    opps = opponents(week)
    inj = espn_injuries()
    from .starts import TEAM_ALIASES

    pool = []
    for raw in players:
        pos = (raw.get("position") or "").upper()
        pos = "DST" if pos in ("DEF", "D/ST") else pos
        row = by_key.get(nkey(raw["name"], pos)) or {}
        pl = {**row, **raw, "position": pos}
        pl.setdefault("pos_rank", pos)
        # ESPN's roster reader has no pro-team field, so `raw["team"]` is None
        # and the dict merge above wipes out the board row's real team. That
        # made every ESPN player look like he was on bye -- the whole family
        # lineup read "BYE" against live week-1 games. Never let a null from one
        # source overwrite a value from another.
        if not pl.get("team"):
            pl["team"] = row.get("team")
        if not pl.get("team") and blob:
            # Last resort for kickers and defenses on ESPN: they have neither a
            # roster team nor a board row (ff.special keeps K/DST off the
            # board), so without this they had no team, no opponent and -- for a
            # defense, whose points are keyed by team -- a projection of zero.
            pl["team"] = _team_from_blob(blob, raw["name"], pos)
        team = (pl.get("team") or "").upper()
        if pos == "DST":
            pts, sp = dsts.get(team, 0.0), 0.0
        else:
            pts, _n, sp = wp.get(nkey(raw["name"], pos), (0.0, 0, 0.0))
        # Full precision, rounded only at render. Rounding each player to 1dp
        # and then summing gave a team total 0.1 adrift of winprob's, which sums
        # raw -- the same lineup showing two different numbers on two tabs.
        pl["week_points"], pl["wk_spread"] = pts, round(sp, 1)
        # Feed codes and nflverse codes disagree; resolve before lookup or LAR
        # and JAC come back with no opponent at all.
        pl["opponent"] = opps.get(TEAM_ALIASES.get(team, team), "BYE")
        if pos in ("K", "DST"):
            pl["status"], pl["why"] = "ok", ""      # no injury feed for either
        else:
            pl["status"], pl["why"] = availability(
                blob or {}, raw["name"], pos, raw.get("team"), week, pts)
            # Second opinion. Sleeper's blob said ACTIVE for a player ESPN had
            # DOUBTFUL, so trust whichever source is more pessimistic.
            e = inj.get(nkey(raw["name"], pos))
            if e:
                worse = ("out" if e.split()[0] in
                         ("Out", "Doubtful", "Injury", "Suspension") else "risk")
                rank = {"ok": 0, "risk": 1, "unknown": 1, "out": 2}
                if rank.get(worse, 0) > rank.get(pl["status"], 0):
                    pl["status"] = worse
                    pl["why"] = f"ESPN: {e}"
                elif pl["status"] == "ok":
                    pl["why"] = f"ESPN: {e}"
        pool.append(pl)
    return pool


def outdoor_games(week: int, season: int = SEASON) -> dict:
    """{team: True} for teams playing outdoors this week."""
    s = schedule(season)
    wk = s[s.week == week]
    out = {}
    for _, g in wk.iterrows():
        outdoor = str(g.get("roof", "")).lower() == "outdoors"
        out[g.home_team] = outdoor
        out[g.away_team] = outdoor
    return out


_INJURY_INDEX: dict = {}


def injury_index(blob: dict) -> dict:
    """{(name,pos): (status, body_part)} built once -- the blob is 12k players
    and a linear scan per lookup is far too slow across five rosters."""
    global _INJURY_INDEX
    if _INJURY_INDEX:
        return _INJURY_INDEX
    idx = {}
    for p in blob.values():
        pos = p.get("position")
        if pos not in ("QB", "RB", "WR", "TE"):
            continue
        nm = f"{p.get('first_name','')} {p.get('last_name','')}".strip()
        if nm:
            idx[key(nm, pos)] = (p.get("injury_status"), p.get("injury_body_part"))
    _INJURY_INDEX = idx
    return idx


def availability(blob: dict, name: str, pos: str, team: str | None,
                 week: int, projected: float | None = None) -> tuple[str, str]:
    """('ok'|'risk'|'out'|'unknown', reason) for one player.

    A player on bye projects 0.0, which is correct but looks identical to a
    missing projection. Distinguish them explicitly -- an unexplained 0.0 reads
    as a broken model and will get second-guessed on a Sunday.
    """
    if team and team in bye_teams(week):
        return "out", f"bye week ({team})"
    st, part = injury_index(blob).get(key(name, pos), (None, None))
    if st in OUT_STATUSES:
        return "out", st + (f" ({part})" if part else "")
    if st in RISK_STATUSES:
        return "risk", st + (f" ({part})" if part else "")
    if projected is not None and projected <= 0:
        return "unknown", "no projection for this week"
    return "ok", ""


# ---------------------------------------------------------------------------
# optimiser
# ---------------------------------------------------------------------------
def optimize(players: list[dict], league) -> tuple[dict, list[dict]]:
    """Best legal lineup. Returns ({slot: [players]}, bench).

    Greedy by projected points, filling dedicated slots before flex. With the
    slot counts these leagues use, greedy is optimal: a player who is best at a
    dedicated slot can never be better used in a flex that a lesser player of
    the same position could fill instead.
    """
    open_slots = dict(league.starters)
    dedicated = [s for s in open_slots if len(SLOT_ELIGIBILITY.get(s, {s})) == 1]
    flexes = [s for s in open_slots if len(SLOT_ELIGIBILITY.get(s, {s})) > 1]
    filled = {s: [] for s in open_slots}
    used = set()

    for p in sorted(players, key=lambda x: -x.get("week_points", 0)):
        if p.get("status") in ("out", "unknown"):
            continue
        for slot in dedicated + flexes:
            if open_slots.get(slot, 0) > 0 and \
                    p["position"] in SLOT_ELIGIBILITY.get(slot, {slot}):
                open_slots[slot] -= 1
                filled[slot].append(p)
                used.add(id(p))
                break
    bench = [p for p in players if id(p) not in used]
    return filled, bench


def close_calls(filled: dict, bench: list[dict], league,
                margin: float = 1.5) -> list[dict]:
    """Starter/bench pairs within `margin` points — the decisions worth a look."""
    out = []
    for slot, starters in filled.items():
        elig = SLOT_ELIGIBILITY.get(slot, {slot})
        alts = [b for b in bench
                if b["position"] in elig and b.get("status") != "out"]
        for s in starters:
            for a in alts:
                gap = s.get("week_points", 0) - a.get("week_points", 0)
                if 0 <= gap <= margin:
                    out.append({"slot": slot, "starting": s, "alternative": a,
                                "gap": round(gap, 1)})
    out.sort(key=lambda x: x["gap"])
    return out

def actual_vs_optimal(pool: list[dict], filled: dict, actual: list[dict]) -> dict:
    """What you have set on the platform, versus what the maths wants.

    The app could previously only show its own optimal lineup, which meant it
    could not answer the question you actually have on Sunday morning -- "is the
    lineup I already set wrong, and what do I change" -- and gave no sign when
    a doubtful player was still sitting in a starting slot.
    """
    from .names import key as nkey

    by_name = {nkey(p["name"], p["position"]): p for p in pool}
    started, missing = [], []
    for a in actual:
        if not a.get("name"):
            missing.append(a.get("slot"))
            continue
        pos = a.get("position") or ""
        pos = "DST" if pos in ("DEF", "D/ST") else pos
        row = by_name.get(nkey(a["name"], pos))
        started.append({**(row or {}), "name": a["name"], "slot": a.get("slot"),
                        "position": pos,
                        "week_points": (row or {}).get("week_points", 0.0),
                        "status": (row or {}).get("status", "unknown"),
                        "why": (row or {}).get("why", "")})
    opt = [p for v in filled.values() for p in v]
    opt_names = {nkey(p["name"], p["position"]) for p in opt}
    cur_names = {nkey(p["name"], p["position"]) for p in started}

    bench_in = sorted((p for p in opt if nkey(p["name"], p["position"]) not in cur_names),
                      key=lambda p: -p.get("week_points", 0))
    sit_out = sorted((p for p in started if nkey(p["name"], p["position"]) not in opt_names),
                     key=lambda p: p.get("week_points", 0))
    swaps = []
    for out_p, in_p in zip(sit_out, bench_in):
        swaps.append({
            "out": out_p["name"], "out_pts": round(out_p.get("week_points", 0), 1),
            "out_slot": out_p.get("slot", ""), "out_status": out_p.get("status", ""),
            "out_why": out_p.get("why", ""),
            "in": in_p["name"], "in_pts": round(in_p.get("week_points", 0), 1),
            "gain": round(in_p.get("week_points", 0) - out_p.get("week_points", 0), 1)})

    # A player who is both unavailable AND already being swapped out is ONE
    # problem, not two. Reporting "Bowers is OUT" and "bench Bowers for Likely"
    # as separate items made the to-do list twice as long as the actual work and
    # buried the instruction under the diagnosis.
    swapped_out = {nkey(sw["out"], "") [0] for sw in swaps}
    broken = [b for b in started
              if (b.get("status") in ("out", "unknown")
                  or (b.get("week_points", 0) <= 0 and b["position"] != "DST"))]
    unresolved = [b for b in broken
                  if nkey(b["name"], "")[0] not in swapped_out]
    return {
        "actual": started,
        "actual_total": round(sum(p.get("week_points", 0) for p in started), 1),
        "optimal_total": round(sum(p.get("week_points", 0) for p in opt), 1),
        "swaps": swaps,
        "empty_slots": missing,
        # only the ones no swap already covers
        "broken": unresolved,
        # every broken starter, including ones a swap already fixes -- the
        # lineup table uses this to paint rows red
        "all_broken": broken,
    }
