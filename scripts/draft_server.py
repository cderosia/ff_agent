#!/usr/bin/env python3
"""Live draft board in a browser.

    python3 scripts/draft_server.py --league 719
    then open http://localhost:8777

Serves a single page that polls a JSON endpoint, so it updates in place with no
reload flicker. Readable across the room, on a tablet, or on your phone via the
LAN address it prints at startup.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from ff import board, draft                    # noqa: E402
from ff.leagues import _env, load_all          # noqa: E402
from ff.names import key                       # noqa: E402
from ff.projections import fetch               # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE: dict = {"ready": False}
LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# draft position maths
# ---------------------------------------------------------------------------
def snake_picks_for_slot(slot: int, teams: int, rounds: int) -> list[int]:
    out = []
    for rd in range(1, rounds + 1):
        pos = slot if rd % 2 == 1 else (teams - slot + 1)
        out.append((rd - 1) * teams + pos)
    return out


def my_slot(L, cfg, me) -> int | None:
    if L.platform == "sleeper":
        d = requests.get(f"https://api.sleeper.app/v1/draft/{L.raw['draft_id']}",
                         timeout=20).json()
        order = d.get("draft_order") or {}
        return order.get(str(me)) or order.get(me)
    if L.platform == "espn":
        cookies = {"espn_s2": _env("ESPN_S2"), "SWID": _env("ESPN_SWID")}
        rd1 = draft.espn_draft_order(L.league_id, cookies)
        for pick_no, team_id in rd1.items():
            if team_id == me:
                return pick_no
    return None


# ---------------------------------------------------------------------------
# poller
# ---------------------------------------------------------------------------
def poll(L, cfg, rows, id_to_player, me, slot, rounds, interval):
    while True:
        try:
            if L.platform == "sleeper":
                picks = draft.sleeper_picks(L.raw["draft_id"])
            else:
                cookies = {"espn_s2": _env("ESPN_S2"), "SWID": _env("ESPN_SWID")}
                picks = draft.espn_picks(L.league_id, cookies, id_to_player)

            taken = {key(p["name"], p["position"]) for p in picks if p["name"]}
            by_key = {key(r["name"], r["position"]): r for r in rows}
            mine = [by_key[key(p["name"], p["position"])] for p in picks
                    if p.get("by") == me and key(p["name"], p["position"]) in by_key]

            on_clock = len(picks) + 1
            my_picks = snake_picks_for_slot(slot, L.teams, rounds) if slot else []
            upcoming = [p for p in my_picks if p >= on_clock]
            until = (upcoming[0] - on_clock) if upcoming else None
            # how many picks between this one and the one after it
            gap = (upcoming[1] - upcoming[0]) if len(upcoming) > 1 else L.teams

            recs = draft.recommend(rows, L, taken, mine, limit=12)
            avail = [r for r in rows if key(r["name"], r["position"]) not in taken]

            value = sorted((r for r in avail
                            if r.get("edge") is not None and r["vbd_rank"] <= 170),
                           key=lambda r: -r["edge"])[:8]
            # who won't survive until your following pick
            horizon = on_clock + (until or 0) + gap
            gone = sorted((r for r in avail if r.get("adp_rank")
                           and r["adp_rank"] < horizon),
                          key=lambda r: r["adp_rank"])[:10]

            with LOCK:
                STATE.update(
                    ready=True, error=None, league=L.name, teams=L.teams,
                    platform=L.platform, on_clock=on_clock, until=until,
                    slot=slot, npicks=len(picks),
                    roster=[{"n": p["name"], "p": p["pos_rank"]} for p in mine],
                    gaps=draft.roster_gaps(mine, L),
                    recs=[{"n": r["name"], "p": r["pos_rank"], "g": r["marginal"],
                           "v": r["vorp"], "e": r.get("edge"),
                           "m": r.get("adp_rank"), "s": r.get("rel_spread", 0)}
                          for r in recs],
                    value=[{"n": r["name"], "p": r["pos_rank"], "e": r["edge"],
                            "m": r["adp_rank"]} for r in value],
                    gone=[{"n": r["name"], "p": r["pos_rank"]} for r in gone],
                    last=[{"n": p["name"], "p": p.get("position"), "no": p["pick_no"]}
                          for p in picks[-6:]][::-1],
                    ts=time.strftime("%H:%M:%S"),
                )
        except Exception as e:
            with LOCK:
                STATE["error"] = f"{type(e).__name__}: {e}"
        time.sleep(interval)


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>draft</title><style>
:root{--bg:#0f1115;--fg:#e8eaed;--dim:#8b93a1;--card:#171a21;--line:#252a34;
--go:#4ade80;--warn:#fbbf24;--bad:#f87171;--acc:#60a5fa}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;padding:14px}
h1{font-size:19px;margin:0 0 2px;letter-spacing:.5px}
.sub{color:var(--dim);font-size:13px;margin-bottom:14px}
.live{color:var(--go)} .stale{color:var(--bad)}
.card{background:var(--card);border:1px solid var(--line);border-radius:9px;
padding:12px 14px;margin-bottom:12px}
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
.big{font-size:26px;font-weight:600}
.need{display:inline-block;background:#2a1f14;color:var(--warn);border-radius:5px;
padding:2px 8px;margin-right:6px;font-size:13px}
.gone span,.roster span{display:inline-block;margin:0 10px 5px 0;color:var(--dim)}
.err{background:#2a1416;border-color:#4b1d22;color:#fca5a5}
@media(max-width:640px){body{padding:8px}td{padding:3px 4px}}
</style></head><body>
<div id=app>loading…</div>
<script>
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const pos=p=>`<span class="pos ${(p||'').replace(/[0-9]/g,'')}">${esc(p)}</span>`;
const sg=v=>v==null?'':`<span class="${v>0?'up':v<0?'dn':''}">${v>0?'+':''}${v}</span>`;
function render(d){
 if(!d.ready) return '<div class=card>waiting for draft data…</div>';
 const mx=Math.max(1,...d.recs.map(r=>r.g));
 let h=`<h1>${esc(d.league)} · pick ${d.on_clock}</h1><div class=sub>`;
 h+= d.until===0 ? `<span class=live>● YOU ARE ON THE CLOCK</span>`
   : d.until==null ? `<span class=dim>draft not started / slot unknown</span>`
   : `you're up in <b>${d.until}</b> pick${d.until==1?'':'s'}`;
 h+=` · slot ${d.slot??'?'} · ${d.npicks} picks in · ${esc(d.ts)}</div>`;
 if(d.error) h+=`<div class="card err">poller: ${esc(d.error)}</div>`;

 h+=`<div class=card><div class=lbl>your roster (${d.roster.length})</div><div class=roster>`;
 h+= d.roster.length? d.roster.map(r=>`<span>${esc(r.n)} ${pos(r.p)}</span>`).join('') : '<span>—</span>';
 h+=`</div><div style="margin-top:9px">`;
 const g=Object.entries(d.gaps);
 h+= g.length? g.map(([s,n])=>`<span class=need>${esc(s)} ×${n}</span>`).join('')
    : `<span class=need style="background:#12240f;color:var(--go)">starters full</span>`;
 h+=`</div></div>`;

 h+=`<div class=card><div class=lbl>take now — value to your lineup</div><table>`;
 for(const r of d.recs){
  h+=`<tr><td style="width:52%"><span class=bar style="width:${Math.round(r.g/mx*70)}px"></span>`
   +`${esc(r.n)} ${pos(r.p)}${r.s>0.2?' <span title="sources disagree" style="color:var(--warn)">◆</span>':''}</td>`
   +`<td class=num style="color:var(--acc)">+${Math.round(r.g)}</td>`
   +`<td class="num pos">vorp ${Math.round(r.v)}</td>`
   +`<td class=num>mkt ${r.m??'—'}</td><td class=num>${sg(r.e)}</td></tr>`;
 }
 h+=`</table></div>`;

 h+=`<div class=card><div class=lbl>value — falling past their price</div><table>`;
 for(const r of d.value)
  h+=`<tr><td>${esc(r.n)} ${pos(r.p)}</td><td class=num>mkt ${r.m}</td><td class=num>${sg(r.e)}</td></tr>`;
 h+=`</table></div>`;

 h+=`<div class=card><div class=lbl>gone before your next pick</div><div class=gone>`;
 h+= d.gone.length? d.gone.map(r=>`<span>${esc(r.n)} ${pos(r.p)}</span>`).join('') : '<span>—</span>';
 h+=`</div></div>`;

 h+=`<div class=card><div class=lbl>last picks</div><div class=gone>`;
 h+= d.last.map(r=>`<span>${r.no}. ${esc(r.n)} ${pos(r.p)}</span>`).join('')||'<span>—</span>';
 return h+`</div></div>`;
}
async function tick(){
 try{const d=await (await fetch('/state')).json();
     document.getElementById('app').innerHTML=render(d);}
 catch(e){}
 setTimeout(tick,4000);
}
tick();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/state"):
            with LOCK:
                body = json.dumps(STATE).encode()
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
    ap.add_argument("--league", required=True)
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--interval", type=float, default=5.0)
    args = ap.parse_args()

    cfgs = {c["name"]: c for c in
            yaml.safe_load((ROOT / "leagues" / "leagues.yaml").read_text())["leagues"]}
    proj = fetch()
    leagues, _ = load_all()
    L = next((x for x in leagues if x.name == args.league), None)
    if L is None:
        sys.exit(f"no league {args.league!r}")
    cfg = cfgs.get(L.name, {})
    rows, _ = board.build(proj, L)
    id_to_player = {p["espn_id"]: p for p in proj if p.get("espn_id")}

    if L.platform == "espn":
        from draft import my_espn_team  # type: ignore
        me = my_espn_team(L.league_id,
                          {"espn_s2": _env("ESPN_S2"), "SWID": _env("ESPN_SWID")})
    else:
        me = cfg.get("owner_id")

    slot = None
    try:
        slot = my_slot(L, cfg, me)
    except Exception:
        pass
    rounds = (L.starter_slots + L.bench)

    threading.Thread(target=poll, daemon=True,
                     args=(L, cfg, rows, id_to_player, me, slot, rounds,
                           args.interval)).start()

    ip = "localhost"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    print(f"  {L.name}: draft slot {slot or '?'} · polling every {args.interval:g}s")
    print(f"  ->  http://localhost:{args.port}")
    print(f"  ->  http://{ip}:{args.port}   (phone / tablet on same wifi)")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
