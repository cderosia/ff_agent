"""The house style: terminal.

Five treatments were built and rendered against live data so the choice could be
made by looking rather than by imagining; Carter picked this one, so the other
four are gone. Data-first: monospace numerals, tight rules, almost no chrome,
and colour used only where it carries meaning.

Three decisions are settled and should not drift:
  * comfortable density -- balanced row height, clear separation
  * semantic colour -- green favoured, red trouble, amber a close call
  * near-black ground with a single green accent; everything else is greyscale

`css()` styles the hand-built blocks; `global_css()` drags Streamlit's own
widgets (tables, metrics, alerts, sidebar, tabs) into the same language so the
page doesn't read as two designs stitched together.
"""
from __future__ import annotations

# Semantic colours. Green and red differ in LIGHTNESS as well as hue, so they
# stay distinguishable with the common red/green colour vision deficiency.
GOOD = "#4ade80"
BAD = "#f87171"
WARN = "#fbbf24"

TERMINAL = {
    "label": "Terminal",
    "bg": "#0b0d10", "panel": "#111419", "line": "#1e242e",
    "text": "#e6e9ef", "dim": "#78829a", "accent": "#4ade80",
    "font": "ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas, monospace",
    "head_font": "ui-monospace, 'SF Mono', SFMono-Regular, Menlo, monospace",
    "radius": "2px", "big": "34px", "cap": "11px",
    "upper": True, "track": "0.08em", "card_border": "1px solid #1e242e",
    "row_pad": "10px 12px",
}

THEMES = {"terminal": TERMINAL}
DEFAULT = TERMINAL


def get(_name: str | None = None) -> dict:
    """One theme now. Kept as a function so callers didn't all have to change."""
    return TERMINAL


def css(t: dict | None = None, scope: str = "") -> str:
    t = t or TERMINAL
    s = f".{scope} " if scope else ""
    up = "uppercase" if t["upper"] else "none"
    return f"""
<style>
{s}.ffwrap {{ background:{t['bg']}; color:{t['text']};
  font-family:{t['font']}; border-radius:{t['radius']};
  padding:20px 22px; border:{t['card_border']}; }}
{s}.ffwrap h2 {{ font-family:{t['head_font']}; font-size:20px; margin:0 0 2px;
  text-transform:{up}; letter-spacing:{t['track']}; color:{t['text']}; }}
{s}.ffsub {{ color:{t['dim']}; font-size:{t['cap']}; margin-bottom:18px;
  text-transform:{up}; letter-spacing:{t['track']}; }}
{s}.ffrow {{ display:flex; gap:14px; margin-bottom:20px; flex-wrap:wrap; }}
{s}.ffstat {{ flex:1; min-width:120px; background:{t['panel']};
  border:{t['card_border']}; border-radius:{t['radius']}; padding:14px 16px; }}
{s}.ffstat .v {{ font-size:{t['big']}; line-height:1.05; font-weight:700;
  font-family:{t['head_font']}; font-variant-numeric:tabular-nums; }}
{s}.ffstat .k {{ font-size:{t['cap']}; color:{t['dim']}; margin-top:6px;
  text-transform:{up}; letter-spacing:{t['track']}; }}
{s}.fftable {{ width:100%; border-collapse:collapse; font-size:14px; }}
{s}.fftable th {{ text-align:left; font-size:{t['cap']}; color:{t['dim']};
  text-transform:{up}; letter-spacing:{t['track']}; font-weight:600;
  padding:8px 12px; border-bottom:1px solid {t['line']}; }}
{s}.fftable td {{ padding:{t['row_pad']}; border-bottom:1px solid {t['line']}; }}
{s}.fftable tr:last-child td {{ border-bottom:none; }}
{s}.ffnum {{ text-align:right; font-variant-numeric:tabular-nums;
  font-family:{t['font']}; font-weight:600; }}
{s}.ffslot {{ color:{t['dim']}; font-size:12px; text-transform:{up};
  letter-spacing:{t['track']}; }}
{s}.good {{ color:{GOOD}; }} {s}.bad {{ color:{BAD}; }}
{s}.warn {{ color:{WARN}; }} {s}.dim {{ color:{t['dim']}; }}
{s}.pill {{ display:inline-block; padding:2px 8px; border-radius:{t['radius']};
  font-size:10px; font-weight:700; text-transform:uppercase;
  letter-spacing:.06em; }}
{s}.pill.out {{ background:rgba(248,113,113,.15); color:{BAD}; }}
{s}.pill.risk {{ background:rgba(251,191,36,.15); color:{WARN}; }}
{s}.ffalert {{ background:rgba(248,113,113,.09); border-left:2px solid {BAD};
  padding:11px 14px; border-radius:{t['radius']}; margin-bottom:8px;
  font-family:{t['font']}; font-size:13px; }}
{s}.ffwarn {{ background:rgba(251,191,36,.09); border-left:2px solid {WARN};
  padding:11px 14px; border-radius:{t['radius']}; margin-bottom:8px;
  font-family:{t['font']}; font-size:13px; }}
{s}.ffok {{ background:rgba(74,222,128,.09); border-left:2px solid {GOOD};
  padding:11px 14px; border-radius:{t['radius']};
  font-family:{t['font']}; font-size:13px; }}
</style>"""


def global_css(t: dict | None = None) -> str:
    """Pull Streamlit's own widgets into the same language.

    Without this the page is two designs at once: hand-built blocks in the house
    style, and default Streamlit tables and metrics in another. Selectors are
    keyed on data-testid, which is the most stable hook Streamlit exposes.
    """
    t = t or TERMINAL
    return f"""
<style>
  .stApp, [data-testid="stAppViewContainer"] {{ background:{t['bg']}; }}
  html, body, [class*="css"] {{ font-family:{t['font']}; }}
  .block-container {{ padding-top:2.2rem; max-width:1240px; }}

  h1, h2, h3, h4 {{ font-family:{t['head_font']} !important;
    letter-spacing:{t['track']}; text-transform:uppercase;
    color:{t['text']} !important; }}
  h1 {{ font-size:1.5rem !important; }}
  h3 {{ font-size:1.05rem !important; }}
  h4 {{ font-size:.92rem !important; color:{t['dim']} !important; }}

  [data-testid="stSidebar"] {{ background:{t['panel']};
    border-right:1px solid {t['line']}; }}
  [data-testid="stSidebar"] * {{ font-family:{t['font']}; }}

  [data-testid="stTabs"] button {{ font-family:{t['font']};
    text-transform:uppercase; letter-spacing:{t['track']}; font-size:12px; }}
  [data-testid="stTabs"] [aria-selected="true"] {{ color:{t['accent']} !important; }}
  [data-testid="stTabs"] [data-baseweb="tab-highlight"] {{
    background:{t['accent']} !important; }}

  [data-testid="stMetricValue"] {{ font-family:{t['head_font']};
    font-size:1.7rem; font-variant-numeric:tabular-nums; }}
  [data-testid="stMetricLabel"] {{ text-transform:uppercase;
    letter-spacing:{t['track']}; font-size:11px; color:{t['dim']}; }}

  [data-testid="stDataFrame"] {{ border:1px solid {t['line']};
    border-radius:{t['radius']}; }}
  [data-testid="stDataFrame"] * {{ font-family:{t['font']} !important;
    font-variant-numeric:tabular-nums; }}

  [data-testid="stAlert"] {{ border-radius:{t['radius']};
    font-family:{t['font']}; font-size:13px; border-left-width:2px; }}

  .stButton button {{ font-family:{t['font']}; text-transform:uppercase;
    letter-spacing:{t['track']}; font-size:11px; border-radius:{t['radius']};
    border:1px solid {t['line']}; background:{t['panel']}; color:{t['text']}; }}
  .stButton button:hover {{ border-color:{t['accent']};
    color:{t['accent']}; }}

  [data-testid="stCaptionContainer"] {{ color:{t['dim']}; font-size:12px; }}
  hr {{ border-color:{t['line']}; }}
</style>"""
