#!/usr/bin/env python3
"""Draft-day board. One job, no chrome.

    python3 scripts/draft_server.py
    open http://localhost:8777

Serves every configured league behind a tab strip, so you can switch leagues
without restarting. Deliberately separate from the season app: on draft day you
want a glanceable second screen that refreshes itself, not a tool to operate.

Picks are polled on demand per league and cached for a few seconds, so an idle
league costs nothing and switching is instant.
"""
from __future__ import annotations

import argparse
import json
import pathlib
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
from ff.leagues import _env, load_all    # noqa: E402
from ff.names import key                 # noqa: E402
from ff.projections import fetch         # noqa: E402

LOCK = threading.Lock()
CTX: dict = {}          # league name -> prepared board + identity
PICK_CACHE: dict = {}   # league name -> (timestamp, picks)
POLL_SECONDS = 4.0


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
        else:
            me = cfg.get("owner_id")

        slot = None
        try:
            if L.platform == "sleeper":
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

        CTX[L.name] = {"league": L, "rows": rows, "meta": meta, "me": me,
                       "slot": slot, "rounds": L.starter_slots + L.bench,
                       "id_map": {p["espn_id"]: p for p in proj if p.get("espn_id")}}
        print(f"  {L.name:16} {L.teams:>2}tm {L.platform:8} slot {slot or '?'} "
              f"· {meta['source']}")


def picks_for(name):
    """Poll this league's picks, cached briefly so switching is cheap."""
    now = time.time()
    ts, cached = PICK_CACHE.get(name, (0, None))
    if cached is not None and now - ts < POLL_SECONDS:
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


def state_for(name):
    c = CTX[name]
    L, rows = c["league"], c["rows"]
    picks = picks_for(name)
    taken = {key(p["name"], p["position"]) for p in picks if p["name"]}
    by_key = {key(r["name"], r["position"]): r for r in rows}
    mine = [by_key[key(p["name"], p["position"])] for p in picks
            if p.get("by") == c["me"] and key(p["name"], p["position"]) in by_key]

    on_clock = len(picks) + 1
    slot = c["slot"]
    my_picks = snake_picks_for_slot(slot, L.teams, c["rounds"]) if slot else []
    upcoming = [p for p in my_picks if p >= on_clock]
    until = (upcoming[0] - on_clock) if upcoming else None
    gap = (upcoming[1] - upcoming[0]) if len(upcoming) > 1 else L.teams

    recs = draft_mod.recommend(rows, L, taken, mine, limit=12)
    avail = [r for r in rows if key(r["name"], r["position"]) not in taken]
    value = sorted((r for r in avail
                    if r.get("edge") is not None and r["vbd_rank"] <= 170),
                   key=lambda r: -r["edge"])[:8]
    horizon = on_clock + (until or 0) + gap
    gone = sorted((r for r in avail if r.get("adp_rank") and r["adp_rank"] < horizon),
                  key=lambda r: r["adp_rank"])[:10]

    return {
        "league": name, "leagues": list(CTX), "teams": L.teams,
        "on_clock": on_clock, "until": until, "slot": slot, "npicks": len(picks),
        "gaps": {s: n for s, n in draft_mod.roster_gaps(mine, L).items()
                 if s not in ("K", "DST")},
        "roster": [{"n": p["name"], "p": p["pos_rank"]} for p in mine],
        "recs": [{"n": r["name"], "p": r["pos_rank"], "g": r["marginal"],
                  "v": r["vorp"], "e": r.get("edge"), "m": r.get("adp_rank"),
                  "s": r.get("rel_spread", 0)} for r in recs],
        "value": [{"n": r["name"], "p": r["pos_rank"], "e": r["edge"],
                   "m": r["adp_rank"]} for r in value],
        "gone": [{"n": r["name"], "p": r["pos_rank"]} for r in gone],
        "last": [{"n": p["name"], "p": p.get("position"), "no": p["pick_no"]}
                 for p in picks[-6:]][::-1],
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
.pos.RB{color:#86efac}.pos.WR{color:#93c5fd}.pos.TE{color:#fca5a5}.pos.QB{color:#fcd34d}
.up{color:var(--go)}.dn{color:var(--bad)}
.need{display:inline-block;background:#2a1f14;color:var(--warn);border-radius:5px;
padding:2px 8px;margin-right:6px;font-size:13px}
.gone span,.roster span{display:inline-block;margin:0 10px 5px 0;color:var(--dim)}
.err{background:#2a1416;border-color:#4b1d22;color:#fca5a5}
@media(max-width:640px){body{padding:8px}td{padding:3px 4px}}
</style></head><body>
<div id=tabs class=tabs></div><div id=app>loading…</div>
<script>
let LEAGUE=new URLSearchParams(location.search).get('league')||'';
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const pos=p=>`<span class="pos ${(p||'').replace(/[0-9]/g,'')}">${esc(p)}</span>`;
const sg=v=>v==null?'':`<span class="${v>0?'up':v<0?'dn':''}">${v>0?'+':''}${v}</span>`;
function tabs(d){
 document.getElementById('tabs').innerHTML=d.leagues.map(n=>
  `<div class="tab ${n===d.league?'on':''}" onclick="pick('${esc(n)}')">${esc(n)}</div>`).join('');
}
function pick(n){LEAGUE=n;history.replaceState({},'',`?league=${encodeURIComponent(n)}`);tick();}
function render(d){
 const mx=Math.max(1,...d.recs.map(r=>r.g));
 let h=`<h1>${esc(d.league)} · pick ${d.on_clock}</h1><div class=sub>`;
 h+= d.until===0?`<span class=live>● YOU ARE ON THE CLOCK</span>`
   : d.until==null?`draft not started / slot unknown`
   : `you're up in <span class=clock>${d.until}</span> pick${d.until==1?'':'s'}`;
 h+=` · slot ${d.slot??'?'} · ${d.npicks} picks in · ${esc(d.ts)}</div>`;
 h+=`<div class=card><div class=lbl>your roster (${d.roster.length})</div><div class=roster>`;
 h+= d.roster.length?d.roster.map(r=>`<span>${esc(r.n)} ${pos(r.p)}</span>`).join(''):'<span>—</span>';
 h+=`</div><div style="margin-top:9px">`;
 const g=Object.entries(d.gaps);
 h+= g.length?g.map(([s,n])=>`<span class=need>${esc(s)} ×${n}</span>`).join('')
    :`<span class=need style="background:#12240f;color:var(--go)">starters full</span>`;
 h+=`</div></div><div class=card><div class=lbl>take now — value to your lineup</div><table>`;
 for(const r of d.recs){
  h+=`<tr><td style="width:52%"><span class=bar style="width:${Math.round(r.g/mx*70)}px"></span>`
   +`${esc(r.n)} ${pos(r.p)}${r.s>0.2?' <span title="sources disagree" style="color:var(--warn)">◆</span>':''}</td>`
   +`<td class=num style="color:var(--acc)">+${Math.round(r.g)}</td>`
   +`<td class="num pos">vorp ${Math.round(r.v)}</td>`
   +`<td class=num>mkt ${r.m??'—'}</td><td class=num>${sg(r.e)}</td></tr>`;}
 h+=`</table></div><div class=card><div class=lbl>value — falling past their price</div><table>`;
 for(const r of d.value)
  h+=`<tr><td>${esc(r.n)} ${pos(r.p)}</td><td class=num>mkt ${r.m}</td><td class=num>${sg(r.e)}</td></tr>`;
 h+=`</table></div><div class=card><div class=lbl>gone before your next pick</div><div class=gone>`;
 h+= d.gone.length?d.gone.map(r=>`<span>${esc(r.n)} ${pos(r.p)}</span>`).join(''):'<span>—</span>';
 h+=`</div></div><div class=card><div class=lbl>last picks</div><div class=gone>`;
 h+= d.last.map(r=>`<span>${r.no}. ${esc(r.n)} ${pos(r.p)}</span>`).join('')||'<span>—</span>';
 return h+`</div></div>`;
}
async function tick(){
 try{
  const d=await (await fetch('/state?league='+encodeURIComponent(LEAGUE))).json();
  if(d.error){document.getElementById('app').innerHTML=
     `<div class="card err">${esc(d.error)}</div>`;}
  else{LEAGUE=d.league;tabs(d);document.getElementById('app').innerHTML=render(d);}
 }catch(e){}
 clearTimeout(window._t);window._t=setTimeout(tick,5000);
}
tick();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
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
    args = ap.parse_args()

    cfgs = {c["name"]: c for c in
            yaml.safe_load((ROOT / "leagues" / "leagues.yaml").read_text())["leagues"]}
    leagues, errors = load_all()
    print("preparing boards…")
    prepare(leagues, cfgs, fetch())
    for nm, err in errors:
        print(f"  skip {nm}: {err[:60]}")

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
