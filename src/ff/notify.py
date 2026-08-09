"""Email delivery for the weekly report.

Email HTML is not web HTML. Gmail and friends strip <style> blocks
unpredictably, drop CSS custom properties, and ignore media queries, so
everything here is inline-styled on the element and laid out with tables.
No variables, no classes, no dark-mode tokens -- those all silently degrade in
a mail client and you'd never see it until the report looked broken on a phone.
"""
from __future__ import annotations

import os
import pathlib
import smtplib
from email.message import EmailMessage

ROOT = pathlib.Path(__file__).resolve().parents[2]

# A palette that survives both light and dark mail clients without media
# queries: mid-tone accents readable on white and on dark grey alike.
INK = "#14171F"
DIM = "#5C6675"
LINE = "#DFE3EA"
GAIN = "#0B6E5B"
COIN = "#8A5D18"
LOSS = "#9E3341"
CARD = "#FFFFFF"


def _env(key: str, default: str = "") -> str:
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip()
    return os.environ.get(key, default)


def _cell(content, align="left", color=INK, weight="400", mono=False, size="14px"):
    fam = ("ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" if mono
           else "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif")
    return (f'<td style="padding:8px 10px;text-align:{align};color:{color};'
            f'font-weight:{weight};font-family:{fam};font-size:{size};'
            f'border-bottom:1px solid {LINE};white-space:nowrap">{content}</td>')


def _table(headers, rows, aligns=None):
    aligns = aligns or ["left"] + ["right"] * (len(headers) - 1)
    head = "".join(
        f'<th style="padding:6px 10px;text-align:{a};color:{DIM};font-size:10px;'
        f'letter-spacing:.08em;text-transform:uppercase;'
        f'font-family:-apple-system,Helvetica,Arial,sans-serif;'
        f'border-bottom:2px solid {LINE}">{h}</th>'
        for h, a in zip(headers, aligns))
    body = "".join(f"<tr>{''.join(r)}</tr>" for r in rows)
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" '
            f'style="width:100%;border-collapse:collapse;margin:6px 0 4px">'
            f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")


def _h2(text):
    return (f'<div style="font:700 11px/1.4 -apple-system,Helvetica,Arial,sans-serif;'
            f'letter-spacing:.13em;text-transform:uppercase;color:{DIM};'
            f'margin:28px 0 2px;padding-bottom:7px;border-bottom:1px solid {LINE}">'
            f"{text}</div>")


def _p(text, size="13.5px"):
    return (f'<p style="margin:8px 0 0;color:{DIM};font-size:{size};line-height:1.5;'
            f"font-family:-apple-system,Helvetica,Arial,sans-serif\">{text}</p>")


CALL_COLOR = {"claim": GAIN, "wait": COIN, "skip": DIM, "?": DIM}


def render_email(league, week, claims, movers, trades_fair, drops, replay=False) -> str:
    """Full HTML body for the weekly email."""
    parts = []
    parts.append(
        f'<div style="font:600 24px/1.25 Georgia,serif;color:{INK};margin:0 0 4px">'
        f"Week {week} · {league.name}</div>")
    flag = (' <span style="color:%s">· replay</span>' % COIN) if replay else ""
    parts.append(
        f'<div style="color:{DIM};font-size:12.5px;'
        f'font-family:-apple-system,Helvetica,Arial,sans-serif">'
        f"{league.teams}-team · waivers {league.waiver_note}{flag}</div>")

    # --- the one call -------------------------------------------------------
    if claims:
        top = claims[0]
        col = CALL_COLOR.get(top["call"], DIM)
        parts.append(
            f'<div style="margin:22px 0 0;padding:16px 18px;background:{CARD};'
            f'border:1px solid {LINE};border-left:3px solid {col};border-radius:4px">'
            f'<div style="font:700 10px/1 -apple-system,Helvetica,Arial,sans-serif;'
            f'letter-spacing:.12em;text-transform:uppercase;color:{DIM}">'
            f'Top move — {top["call"]}</div>'
            f'<div style="font:600 20px/1.3 Georgia,serif;color:{INK};margin-top:6px">'
            f'{top["name"]} <span style="color:{DIM};font-size:13px">{top["pos"]}</span></div>'
            f'<div style="color:{DIM};font-size:13.5px;margin-top:4px;'
            f'font-family:-apple-system,Helvetica,Arial,sans-serif">'
            f'Adds <b style="color:{GAIN}">+{top["gain"]:.0f}</b> to your projected '
            f'starting lineup. {top["why"]}.</div></div>')
    else:
        parts.append(
            f'<div style="margin:22px 0 0;padding:16px 18px;background:{CARD};'
            f'border:1px solid {LINE};border-radius:4px;color:{DIM};font-size:14px;'
            f'font-family:-apple-system,Helvetica,Arial,sans-serif">'
            f"Nothing on the wire improves your starting lineup. Don't spend a claim.</div>")

    # --- waiver table -------------------------------------------------------
    if claims:
        parts.append(_h2("Worth adding"))
        parts.append(_p("Ranked by what each adds to <i>your</i> starting lineup. "
                        "No money in these leagues — the cost of a claim is your "
                        "waiver position, so most weeks the right move is to wait."))
        rows = []
        for c in claims[:6]:
            col = CALL_COLOR.get(c["call"], DIM)
            rows.append([
                _cell(f'{c["name"]}<span style="color:{DIM};font-size:11px;'
                      f'margin-left:7px">{c["pos"]}</span>'),
                _cell(f'+{c["gain"]:.0f}', "right", GAIN, "700", True),
                _cell(f'{c["ppg"]:.1f}', "right", INK, "400", True),
                _cell(f'{c["snap"]*100:.0f}%', "right", INK, "400", True),
                _cell(f'{c["dsnap"]*100:+.0f}', "right",
                      GAIN if c["dsnap"] > 0 else LOSS, "400", True),
                _cell(c["call"].upper(), "right", col, "700", False, "11px"),
            ])
        parts.append(_table(["Player", "Adds", "PPG", "Snap", "Δ", "Call"], rows))

    # --- usage --------------------------------------------------------------
    if movers:
        parts.append(_h2("Usage risers"))
        parts.append(_p("Opportunity moves before production does — biggest snap and "
                        "target gains among unrostered players."))
        rows = [[
            _cell(f'{m["name"]}<span style="color:{DIM};font-size:11px;'
                  f'margin-left:7px">{m["pos"]}</span>'),
            _cell(f'{m["snap"]*100:.0f}%', "right", INK, "400", True),
            _cell(f'{m["dsnap"]*100:+.0f}', "right",
                  GAIN if m["dsnap"] > 0 else LOSS, "400", True),
            _cell(f'{m["tgt"]:.1f}', "right", INK, "400", True),
            _cell(f'{m["ppg"]:.1f}', "right", INK, "400", True),
        ] for m in movers[:8]]
        parts.append(_table(["Player", "Snap", "Δ", "Tgt", "PPG"], rows))

    # --- trades -------------------------------------------------------------
    if trades_fair:
        parts.append(_h2("Trades worth sending"))
        parts.append(_p("Scored by change in projected starting lineup for "
                        "<i>both</i> teams. <b>Optics</b> is the raw value gap the other "
                        "manager sees — very negative reads as a fleece and gets declined."))
        for t in trades_fair[:4]:
            g = " + ".join(p["name"] for p in t["give"])
            r = " + ".join(p["name"] for p in t["get"])
            parts.append(
                f'<div style="margin:10px 0 0;padding:13px 15px;background:{CARD};'
                f'border:1px solid {LINE};border-left:3px solid {GAIN};border-radius:4px">'
                f'<div style="font:600 14px -apple-system,Helvetica,Arial,sans-serif;'
                f'color:{INK}">{t["team"]}</div>'
                f'<div style="color:{DIM};font-size:13.5px;margin-top:4px;'
                f'font-family:-apple-system,Helvetica,Arial,sans-serif">'
                f'send <b style="color:{INK}">{g}</b> → get <b style="color:{INK}">{r}</b></div>'
                f'<div style="margin-top:7px;font-family:ui-monospace,Menlo,monospace;'
                f'font-size:12.5px;color:{DIM}">'
                f'you <b style="color:{GAIN}">{t["my_delta"]:+.0f}</b> · '
                f'them <b style="color:{GAIN}">{t["their_delta"]:+.0f}</b> · '
                f'optics <b style="color:{INK}">{t["optics"]:+.0f}</b></div></div>')

    # --- drops --------------------------------------------------------------
    if drops:
        parts.append(_h2("Droppable"))
        rows = [[
            _cell(f'{r["name"]}<span style="color:{DIM};font-size:11px;'
                  f'margin-left:7px">{r["pos"]}</span>'),
            _cell(f'{r["vorp"]:.0f}', "right",
                  LOSS if r["vorp"] < 0 else INK, "400", True),
        ] for r in drops[:5]]
        parts.append(_table(["Player", "VORP"], rows, ["left", "right"]))

    parts.append(
        f'<div style="margin-top:30px;padding-top:14px;border-top:1px solid {LINE};'
        f'color:{DIM};font-size:11.5px;'
        f'font-family:-apple-system,Helvetica,Arial,sans-serif">'
        f"Built from your league's own scoring and roster settings, blended ESPN + "
        f"Rotowire projections, and nflverse snap and target data.</div>")

    return (f'<body style="margin:0;padding:0;background:#F2F4F7">'
            f'<div style="max-width:640px;margin:0 auto;padding:26px 18px 40px">'
            f"{''.join(parts)}</div></body>")


def send(subject: str, html: str, to: str | None = None) -> str:
    """Send via SMTP. Returns a status string; raises on hard failure."""
    host = _env("SMTP_HOST", "smtp.gmail.com")
    port = int(_env("SMTP_PORT", "587"))
    user = _env("SMTP_USER")
    pw = _env("SMTP_PASS")
    to = to or _env("REPORT_TO") or user
    if not (user and pw and to):
        raise RuntimeError("set SMTP_USER, SMTP_PASS and REPORT_TO in .env")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content("This report is HTML. Open it in a client that renders HTML.")
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(host, port, timeout=45) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)
    return f"sent to {to}"
