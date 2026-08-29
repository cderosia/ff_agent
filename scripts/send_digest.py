#!/usr/bin/env python3
"""One email covering every league, instead of one per league.

    python3 scripts/send_digest.py --dry-run     # write the HTML, send nothing
    python3 scripts/send_digest.py               # send it

Five separate emails made sense when the reports were Sleeper-only and there
was one. With five leagues across three platforms -- several of which have not
drafted yet -- a single digest is the readable shape: what needs a decision
this week, league by league, in one place.

Reuses weekly_report.build() rather than re-deriving anything, so the email and
the markdown report can never disagree about what your roster is.
"""
from __future__ import annotations

import argparse
import html as html_mod
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from ff import notify, rosters             # noqa: E402
from ff.leagues import load_all            # noqa: E402
from weekly_report import build, render_tuesday   # noqa: E402

OUT = ROOT / "reports"

CSS = """
body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  color:#1a1a1a;background:#fff;margin:0;padding:20px}
.wrap{max-width:720px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:15px;margin:22px 0 6px;padding-top:14px;border-top:1px solid #e4e4e7}
h3{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:#71717a;
  margin:16px 0 6px}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid #f0f0f0}
th{color:#71717a;font-weight:600;font-size:11px;text-transform:uppercase}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
blockquote{margin:10px 0;padding:8px 12px;background:#fef9ec;
  border-left:3px solid #f0c040;color:#5c4a00;font-size:13px}
em{color:#71717a}
.lede{background:#f4f4f5;border-radius:8px;padding:10px 14px;margin:10px 0}
.hd{color:#71717a;font-size:12px;margin-bottom:18px}
"""


def md_to_html(md: str) -> str:
    """Deliberately small: our own markdown, not arbitrary input."""
    out, in_table = [], False
    for line in md.split("\n"):
        s = line.rstrip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue                                  # separator row
            tag = "th" if not in_table else "td"
            if not in_table:
                out.append("<table>")
                in_table = True
            row = "".join(
                f'<{tag} class="num">{c}</{tag}>' if re.fullmatch(r"[-+]?[\d.,%]+", c)
                else f"<{tag}>{c}</{tag}>" for c in cells)
            out.append(f"<tr>{row}</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if not s:
            continue
        if s.startswith("### "):   out.append(f"<h3>{s[4:]}</h3>")
        elif s.startswith("## "):  out.append(f"<h2>{s[3:]}</h2>")
        elif s.startswith("# "):   out.append(f"<h1>{s[2:]}</h1>")
        elif s.startswith("> "):   out.append(f"<blockquote>{s[2:]}</blockquote>")
        elif s.startswith("_") and s.endswith("_"):
            out.append(f"<p><em>{s[1:-1]}</em></p>")
        else:                      out.append(f"<p>{s}</p>")
    if in_table:
        out.append("</table>")
    body = "\n".join(out)
    body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)
    body = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", body)
    return body


def headline(L, d) -> str:
    """One line per league, so the email is scannable before it is readable."""
    o = d.get("odds") or {}
    if o.get("mode") == "guillotine":
        return (f"{o['advance']:.0%} to advance · projected "
                f"{o['projected_rank']} of {o['of']}")
    if o.get("mode") == "head_to_head" and o.get("win") is not None:
        return f"{o['win']:.0%} to beat {o['opponent']}"
    if o.get("mode") == "field":
        return f"projected {o['projected_rank']} of {o['of']}"
    return "no odds available"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--week", type=int, default=1)
    ap.add_argument("--season", type=int, default=2026)
    args = ap.parse_args()

    cfgs = {c["name"]: c for c in
            yaml.safe_load((ROOT / "leagues" / "leagues.yaml").read_text())["leagues"]}
    leagues, errs = load_all()

    parts, summary = [], []
    for L in leagues:
        try:
            d = build(L, cfgs.get(L.name, {}), args.season, args.week, True)
        except Exception as e:
            summary.append(f"<li><strong>{html_mod.escape(L.name)}</strong> — "
                           f"failed: {html_mod.escape(type(e).__name__)}</li>")
            print(f"  FAIL {L.name}: {type(e).__name__}: {e}")
            continue
        drafted = rosters.has_drafted(L, d.get("teams") or [])
        note = headline(L, d) if drafted else "not drafted yet"
        summary.append(f"<li><strong>{html_mod.escape(L.name)}</strong> — {note}</li>")
        if not drafted:
            continue                       # nothing to say about an empty roster
        parts.append(md_to_html(render_tuesday(L, d, args.season, args.week, True)))
        print(f"  {L.name}: {note}")

    for nm, e in errs:
        summary.append(f"<li><strong>{html_mod.escape(nm)}</strong> — "
                       f"config error</li>")

    body = (f"<div class=wrap><h1>Week {args.week}</h1>"
            f"<div class=hd>all leagues, one email</div>"
            f"<div class=lede><ul>{''.join(summary)}</ul></div>"
            + "\n".join(parts) + "</div>")
    html = f"<html><head><meta charset='utf-8'><style>{CSS}</style></head>" \
           f"<body>{body}</body></html>"

    OUT.mkdir(exist_ok=True)
    path = OUT / f"digest-w{args.week}.html"
    path.write_text(html)
    print(f"\n  -> {path.relative_to(ROOT)}  ({len(html):,} bytes)")
    if args.dry_run:
        print("  dry run — nothing sent")
    else:
        print("  " + notify.send(f"Week {args.week} · all leagues", html))


if __name__ == "__main__":
    main()
