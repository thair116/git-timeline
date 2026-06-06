"""M7: Render the timeline as an HTML site.

Scans cache/ for all analyzed repos, emits:
  output/site/index.html            — multi-repo dashboard
  output/site/<repo>/index.html     — one page per repo (self-contained)
  output/site/assets/style.css      — shared stylesheet
  output/site/assets/app.js         — shared interactivity

Everything is driven from SQLite. No server; open index.html in a browser.

Usage:
    python -m src.render
"""
from __future__ import annotations

import argparse
import colorsys
import hashlib
import html
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from . import db as db_mod
from . import paths
from . import wrapped as wrapped_mod


# ---------------------------------------------------------------------------
# Minimal markdown renderer (tuned to what the synthesizer emits).
# ---------------------------------------------------------------------------

def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def md_to_html(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if ln.startswith("### "):
            out.append(f"<h3>{_inline(ln[4:])}</h3>")
            i += 1
        elif ln.startswith("## "):
            out.append(f"<h2>{_inline(ln[3:])}</h2>")
            i += 1
        elif ln.startswith("# "):
            out.append(f"<h1>{_inline(ln[2:])}</h1>")
            i += 1
        elif ln.startswith("|"):
            tbl = []
            while i < len(lines) and lines[i].startswith("|"):
                tbl.append(lines[i])
                i += 1
            out.append(_render_table(tbl))
        elif ln.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(f"<li>{_inline(lines[i][2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
        elif ln.strip() == "":
            i += 1
        else:
            # Accumulate paragraph lines.
            buf = [ln]
            i += 1
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(
                ("#", "-", "|")
            ):
                buf.append(lines[i].rstrip())
                i += 1
            out.append(f"<p>{_inline(' '.join(buf))}</p>")
    return "\n".join(out)


def _render_table(rows: list[str]) -> str:
    # Separator row looks like "| --- | --- |"
    cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
    if len(cells) >= 2 and all(set(c) <= set("-:") for c in cells[1]):
        header = cells[0]
        body = cells[2:]
    else:
        header = None
        body = cells
    html_parts = ['<div class="table-wrap"><table>']
    if header:
        html_parts.append("<thead><tr>")
        for c in header:
            html_parts.append(f"<th>{_inline(c)}</th>")
        html_parts.append("</tr></thead>")
    html_parts.append("<tbody>")
    for row in body:
        html_parts.append("<tr>")
        for c in row:
            html_parts.append(f"<td>{_inline(c)}</td>")
        html_parts.append("</tr>")
    html_parts.append("</tbody></table></div>")
    return "".join(html_parts)


# ---------------------------------------------------------------------------
# Data loading.
# ---------------------------------------------------------------------------

def load_repo_data(conn: sqlite3.Connection) -> dict | None:
    """Return all the data needed to render one repo, or None if incomplete."""
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    bootstrap = {}
    if meta.get("bootstrap_json"):
        try:
            bootstrap = json.loads(meta["bootstrap_json"])
        except json.JSONDecodeError:
            pass

    totals = conn.execute("""
        SELECT COUNT(*) AS commits,
               MIN(committed_at) AS first_commit,
               MAX(committed_at) AS last_commit,
               SUM(insertions) AS total_ins,
               SUM(deletions) AS total_dels,
               COUNT(DISTINCT author_name) AS authors,
               COUNT(DISTINCT month) AS months
        FROM commits
    """).fetchone()
    if not totals or totals["commits"] == 0:
        return None

    months = []
    rows = list(conn.execute("""
        SELECT
            c.month,
            COUNT(*) AS commits,
            SUM(c.insertions) AS ins,
            SUM(c.deletions) AS dels,
            ms.theme, ms.shipped, ms.abandoned, ms.churn_ratio,
            sv.surviving_lines, sv.survival_ratio, sv.files_remaining
        FROM commits c
        LEFT JOIN month_summaries ms ON ms.month = c.month
        LEFT JOIN month_survival sv ON sv.month = c.month
        GROUP BY c.month
        ORDER BY c.month ASC
    """))
    for r in rows:
        shipped_blob = {}
        shipped = []
        tags = []
        if r["shipped"]:
            try:
                blob = json.loads(r["shipped"])
                if isinstance(blob, dict):
                    shipped = blob.get("shipped", []) or []
                    tags = blob.get("tags", []) or []
                    shipped_blob = blob
            except json.JSONDecodeError:
                pass
        abandoned = []
        if r["abandoned"]:
            try:
                ab = json.loads(r["abandoned"])
                if isinstance(ab, list):
                    abandoned = ab
            except json.JSONDecodeError:
                pass
        kinds = {
            kr["kind"]: kr["n"]
            for kr in conn.execute("""
                SELECT s.kind, COUNT(*) AS n
                FROM commit_summaries s
                JOIN commits c ON c.sha = s.sha
                WHERE c.month = ?
                GROUP BY s.kind
                ORDER BY n DESC
            """, (r["month"],))
        }
        months.append({
            "month": r["month"],
            "commits": r["commits"],
            "ins": r["ins"] or 0,
            "dels": r["dels"] or 0,
            "theme": r["theme"] or "",
            "shipped": shipped,
            "tags": tags,
            "abandoned": abandoned,
            "churn_ratio": r["churn_ratio"] or 0.0,
            "surviving_lines": r["surviving_lines"] or 0,
            "survival_ratio": r["survival_ratio"] if r["survival_ratio"] is not None else None,
            "files_remaining": r["files_remaining"] or 0,
            "kinds": kinds,
        })

    # Synthesis is cached in llm_cache under stage='synthesize'.
    synth_row = conn.execute(
        "SELECT response FROM llm_cache WHERE stage = 'synthesize' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    synthesis_md = synth_row["response"] if synth_row else ""

    # Spend.
    spend_rows = list(conn.execute("""
        SELECT stage, model, COUNT(*) AS n,
               SUM(input_tokens) AS in_t,
               SUM(output_tokens) AS out_t
        FROM llm_cache
        GROUP BY stage, model
    """))

    tree_html = render_tree_svg(conn)

    return {
        "meta": meta,
        "bootstrap": bootstrap,
        "totals": dict(totals),
        "months": months,
        "synthesis_md": synthesis_md,
        "spend_rows": [dict(r) for r in spend_rows],
        "tree_html": tree_html,
    }


# ---------------------------------------------------------------------------
# Viz helpers.
# ---------------------------------------------------------------------------

def repo_hue(name: str) -> int:
    h = hashlib.sha1(name.encode()).digest()
    return int.from_bytes(h[:2], "big") % 360


def verdict_for(survival: float | None) -> str:
    if survival is None:
        return "unknown"
    if survival >= 0.50:
        return "green"
    if survival >= 0.25:
        return "amber"
    return "red"


def hsl(h: int, s: int, l: int) -> str:
    return f"hsl({h} {s}% {l}%)"


def fmt_int(n: int | float | None) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}"


def fmt_pct(x: float | None, *, nd: int = 0) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.{nd}f}%"


def fmt_loc(v: int | float | None) -> str:
    """Compact LoC: 314237 -> 314K, 1183284 -> 1.2M, 942 -> 942."""
    if v is None:
        return "—"
    v = int(v)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 10_000:
        return f"{round(v / 1000)}K"
    if v >= 1_000:
        return f"{v / 1000:.1f}K"
    return str(v)


def fmt_month(m: str) -> str:
    try:
        d = datetime.strptime(m + "-01", "%Y-%m-%d")
        return d.strftime("%b %Y")
    except ValueError:
        return m


# ---------------------------------------------------------------------------
# Stat strip — two semantic clusters with icons; survival as the accent hero.
# ---------------------------------------------------------------------------

# Inline outline icons (1.6px stroke, currentColor) so they tint with the theme.
_ICONS = {
    "commits": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="3.4"/><line x1="3" y1="12" x2="8.6" y2="12"/><line x1="15.4" y1="12" x2="21" y2="12"/></svg>',
    "months": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><rect x="3.5" y="5" width="17" height="15.5" rx="2"/><line x1="3.5" y1="9.5" x2="20.5" y2="9.5"/><line x1="8" y1="3" x2="8" y2="6.5"/><line x1="16" y1="3" x2="16" y2="6.5"/></svg>',
    "authors": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="12" cy="8" r="3.4"/><path d="M5.5 20a6.5 6.5 0 0 1 13 0"/></svg>',
    "loc": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="8.5 7 3.5 12 8.5 17"/><polyline points="15.5 7 20.5 12 15.5 17"/></svg>',
    "written": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/><line x1="14" y1="6" x2="17.5" y2="9.5"/></svg>',
    "halflife": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
}


def _survival_ring(frac: float | None, hue: int) -> str:
    """A small donut showing the survival fraction, accent-tinted."""
    r = 8.0
    circ = 2 * 3.141592653589793 * r
    dash = circ * (frac or 0.0)
    return (
        f'<svg class="ring" viewBox="0 0 22 22" fill="none">'
        f'<circle cx="11" cy="11" r="{r}" stroke="var(--border)" stroke-width="2.4"/>'
        f'<circle cx="11" cy="11" r="{r}" stroke="hsl({hue} 75% 62%)" stroke-width="2.4" '
        f'stroke-linecap="round" stroke-dasharray="{dash:.2f} {circ:.2f}" '
        f'transform="rotate(-90 11 11)"/></svg>'
    )


def _stat(icon: str, value, label: str, *, hero: bool = False) -> str:
    cls = "stat stat-hero" if hero else "stat"
    return (
        f'<div class="{cls}"><div class="top">{icon}'
        f'<span class="n">{value}</span></div>'
        f'<span class="l">{html.escape(label)}</span></div>'
    )


def build_stat_strip(totals: dict, total_loc: int, global_surv: float | None,
                     half_life_disp: str | None = None, hue: int = 200) -> str:
    """Two clusters: Activity (commits/months/authors) and Code (written/current/survival)."""
    lines_written = totals.get("total_ins") or 0
    activity = "".join([
        _stat(_ICONS["commits"], fmt_int(totals.get("commits")), "commits"),
        _stat(_ICONS["months"], totals.get("months", 0), "months"),
        _stat(_ICONS["authors"], totals.get("authors", 0), "authors"),
    ])
    code_stats = [
        _stat(_ICONS["written"], fmt_loc(lines_written), "lines written"),
        _stat(_ICONS["loc"], fmt_loc(total_loc), "current LoC"),
        _stat(_survival_ring(global_surv, hue), fmt_pct(global_surv), "survival", hero=True),
    ]
    if half_life_disp:  # only when the survival_curve stage has run
        code_stats.append(_stat(_ICONS["halflife"], half_life_disp, "code half-life"))
    code = "".join(code_stats)
    return (
        '<div class="stat-strip clustered">'
        f'<div class="stat-group"><div class="sg-label">Activity</div>'
        f'<div class="sg-items">{activity}</div></div>'
        '<div class="stat-div"></div>'
        f'<div class="stat-group"><div class="sg-label">Code</div>'
        f'<div class="sg-items">{code}</div></div>'
        '</div>'
    )


# ---------------------------------------------------------------------------
# Timeline SVG.
# ---------------------------------------------------------------------------

def render_timeline_svg(months: list[dict], hue: int) -> str:
    """Horizontal bar per month. Height = sqrt(lines inserted), fill ratio = survival.

    Bar height tracks code added that month on a square-root scale, so a single
    huge month doesn't crush all the others into invisible slivers. The filled
    portion is still height * survival_ratio (share of inserted code alive at HEAD)
    regardless of the scaling. Coloring: verdict tint (green/amber/red) by hue.
    """
    if not months:
        return ""

    W = 960
    H = 260
    PAD = 24
    LPAD = 52  # wider left gutter so 5-6 digit LoC axis labels fit
    GAP = 4
    LABEL_H = 40
    chart_w = W - LPAD - PAD
    chart_h = H - PAD * 2 - LABEL_H
    n = len(months)
    col_w = (chart_w - GAP * (n - 1)) / max(n, 1)

    max_loc = max(m["ins"] for m in months) or 1

    # Color ramps per verdict.
    def bar_colors(m):
        surv = m["survival_ratio"]
        v = verdict_for(surv)
        if v == "green":
            return hsl(145, 55, 42), hsl(145, 70, 60)
        if v == "amber":
            return hsl(38, 75, 45), hsl(38, 85, 62)
        if v == "red":
            return hsl(2, 55, 45), hsl(2, 70, 62)
        return hsl(hue, 10, 35), hsl(hue, 20, 55)

    svg = [
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
        f'class="timeline" role="img" aria-label="Month timeline">'
    ]
    # Grid lines at even vertical steps. The y-scale is sqrt(LoC), so the LoC
    # value at height-fraction `frac` is max_loc * frac**2 (labels spread out
    # toward the top — that's the visual signature of the sqrt compression).
    for frac in (0.25, 0.5, 0.75, 1.0):
        y = PAD + chart_h * (1 - frac)
        svg.append(
            f'<line x1="{LPAD}" x2="{W - PAD}" y1="{y:.1f}" y2="{y:.1f}" '
            f'class="gridline"/>'
        )
        svg.append(
            f'<text x="{LPAD - 8}" y="{y + 4:.1f}" class="gridlabel" '
            f'text-anchor="end">{fmt_loc(int(max_loc * frac * frac))}</text>'
        )

    for i, m in enumerate(months):
        x = LPAD + i * (col_w + GAP)
        h_full = chart_h * (m["ins"] / max_loc) ** 0.5
        y_full = PAD + chart_h - h_full
        surv = m["survival_ratio"] or 0.0
        h_surv = h_full * surv
        y_surv = PAD + chart_h - h_surv

        c_dim, c_bright = bar_colors(m)

        # Full-height bar (dim = rewritten / lost to HEAD).
        svg.append(
            f'<rect x="{x:.1f}" y="{y_full:.1f}" width="{col_w:.1f}" '
            f'height="{h_full:.1f}" rx="3" fill="{c_dim}" opacity="0.35" '
            f'class="bar-full"/>'
        )
        # Survived portion.
        svg.append(
            f'<rect x="{x:.1f}" y="{y_surv:.1f}" width="{col_w:.1f}" '
            f'height="{h_surv:.1f}" rx="3" fill="{c_bright}" '
            f'class="bar-surv"/>'
        )
        # Hover target + tooltip data (JS-driven).
        tip = (
            f"{fmt_month(m['month'])} · {fmt_int(m['ins'])} LoC · "
            f"{m['commits']} commits · "
            f"survival {fmt_pct(m['survival_ratio'])} · "
            f"{m['theme'][:100]}"
        )
        svg.append(
            f'<a href="#month-{m["month"]}">'
            f'<rect x="{x:.1f}" y="{PAD:.1f}" width="{col_w:.1f}" '
            f'height="{chart_h:.1f}" fill="transparent" '
            f'class="bar-hit" data-tip="{html.escape(tip)}"/></a>'
        )
        # Month label.
        label_y = PAD + chart_h + 16
        lbl = fmt_month(m["month"]).split(" ")
        svg.append(
            f'<text x="{x + col_w / 2:.1f}" y="{label_y}" '
            f'text-anchor="middle" class="monthlabel">{lbl[0]}</text>'
        )
        svg.append(
            f'<text x="{x + col_w / 2:.1f}" y="{label_y + 14}" '
            f'text-anchor="middle" class="monthlabel dim">{lbl[1]}</text>'
        )

    svg.append("</svg>")
    return "\n".join(svg)


def render_survival_curve_svg(curve: list[dict], hue: int,
                             half_life: float | None) -> str:
    """Line+area chart of S(age): share of code still alive `age` months after writing."""
    if len(curve) < 2:
        return ""
    W, H, PAD, LPAD, BPAD = 720, 240, 20, 46, 38
    cw, ch = W - LPAD - PAD, H - PAD - BPAD
    max_age = max(p["age"] for p in curve) or 1
    acc = f"hsl({hue} 75% 62%)"

    def X(age):
        return LPAD + cw * (age / max_age)

    def Y(s):
        return PAD + ch * (1 - s)

    p = [f'<svg viewBox="0 0 {W} {H}" class="survcurve" role="img" '
         f'aria-label="Code survival curve">']
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = Y(frac)
        cls = "gridline mid" if frac == 0.5 else "gridline"
        p.append(f'<line x1="{LPAD}" x2="{W - PAD}" y1="{y:.1f}" y2="{y:.1f}" class="{cls}"/>')
        p.append(f'<text x="{LPAD - 8}" y="{y + 3:.1f}" class="axlabel" '
                 f'text-anchor="end">{int(frac * 100)}%</text>')
    step = max(1, round(max_age / 6))
    age = 0
    while age <= max_age:
        p.append(f'<text x="{X(age):.1f}" y="{H - BPAD + 17:.1f}" class="axlabel" '
                 f'text-anchor="middle">{age}</text>')
        age += step
    p.append(f'<text x="{(LPAD + W - PAD) / 2:.1f}" y="{H - 5:.1f}" class="axtitle" '
             f'text-anchor="middle">months after a line is written</text>')

    pts = [(X(q["age"]), Y(q["survival"])) for q in curve]
    line = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    p.append(f'<path d="{line} L {pts[-1][0]:.1f} {Y(0):.1f} '
             f'L {pts[0][0]:.1f} {Y(0):.1f} Z" fill="{acc}" opacity="0.12"/>')
    p.append(f'<path d="{line}" fill="none" stroke="{acc}" stroke-width="2.2" '
             f'stroke-linejoin="round"/>')
    for x, y in pts:
        p.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="{acc}"/>')
    if half_life is not None and half_life <= max_age:
        hx, hy = X(half_life), Y(0.5)
        p.append(f'<line x1="{hx:.1f}" x2="{hx:.1f}" y1="{Y(0):.1f}" y2="{hy:.1f}" class="hl-line"/>')
        p.append(f'<circle cx="{hx:.1f}" cy="{hy:.1f}" r="4" fill="{acc}" '
                 f'stroke="var(--bg)" stroke-width="1.5"/>')
        p.append(f'<text x="{hx + 7:.1f}" y="{hy - 8:.1f}" class="hl-label">'
                 f'half-life ≈ {half_life:.1f} mo</text>')
    p.append("</svg>")
    return "".join(p)


# ---------------------------------------------------------------------------
# Tech-tree DAG renderer.
# ---------------------------------------------------------------------------

CATEGORY_COLORS = {
    "infra":        "#6b8fb3",
    "backend":      "#a78bfa",
    "data":         "#14b8a6",
    "audio":        "#ec4899",
    "ui":           "#eab308",
    "auth":         "#5bc784",
    "monetization": "#f59e0b",
    "content":      "#06b6d4",
    "gamification": "#f97316",
    "ops":          "#64748b",
    "experiment":   "#d946ef",
    "ios-native":   "#818cf8",
}


def _load_tree(conn) -> tuple[list[dict], list[dict]]:
    nodes = [dict(r) for r in conn.execute(
        "SELECT id, label, category, start_month, end_month, status, description "
        "FROM tree_nodes"
    )]
    edges = [dict(r) for r in conn.execute(
        "SELECT src, dst, kind FROM tree_edges"
    )]
    return nodes, edges


def _layout_tree(
    nodes: list[dict], edges: list[dict], months: list[str]
) -> dict[str, dict]:
    """Assign each node a (start_col, end_col, lane). Greedy lane packing;
    children inherit parent's lane when possible so chains read as trunks."""
    if not months:
        return {}
    month_idx = {m: i for i, m in enumerate(months)}
    n_cols = len(months) + 1  # +1 for phantom HEAD column

    primary_pred: dict[str, str] = {}
    for e in edges:
        if e["kind"] in ("evolved-into", "replaced-by"):
            primary_pred[e["dst"]] = e["src"]

    layout: dict[str, dict] = {}
    for n in nodes:
        start = month_idx.get(n["start_month"], 0)
        if n.get("end_month"):
            end = month_idx.get(n["end_month"], start)
            if end < start:
                end = start
        else:
            end = n_cols - 1
        layout[n["id"]] = {
            "start_col": start, "end_col": end,
            "node": n, "lane": None,
        }

    # Sort by start_col then by status (live chains first for predictability).
    sorted_ids = sorted(
        layout,
        key=lambda i: (
            layout[i]["start_col"],
            0 if layout[i]["node"]["status"] == "live" else 1,
            -layout[i]["end_col"],  # longer spans first
        ),
    )

    occupancy: dict[int, set] = {}

    def fits(lane: int, s: int, e: int) -> bool:
        occ = occupancy.get(lane, set())
        return not any(c in occ for c in range(s, e + 1))

    def assign(lane: int, s: int, e: int) -> None:
        occupancy.setdefault(lane, set()).update(range(s, e + 1))

    for nid in sorted_ids:
        info = layout[nid]
        s, e = info["start_col"], info["end_col"]
        pred = primary_pred.get(nid)
        pref_lane = layout[pred]["lane"] if pred and layout.get(pred, {}).get("lane") is not None else None

        chosen = None
        if pref_lane is not None and fits(pref_lane, s, e):
            chosen = pref_lane
        if chosen is None:
            lane = 0
            while True:
                if fits(lane, s, e):
                    chosen = lane
                    break
                lane += 1
        assign(chosen, s, e)
        info["lane"] = chosen

    return layout


def render_tree_svg(conn) -> str:
    nodes, edges = _load_tree(conn)
    if not nodes:
        return ""

    months = [
        r["month"]
        for r in conn.execute("SELECT DISTINCT month FROM commits ORDER BY month")
    ]
    layout = _layout_tree(nodes, edges, months)
    if not layout:
        return ""

    # Layout geometry.
    COL_W = 78
    LANE_H = 38
    PAD_X = 24
    PAD_TOP = 48
    PAD_BOT = 24
    MIN_W = 120           # minimum node width so 1-col live nodes are readable
    n_cols = len(months) + 1
    max_lane = max(info["lane"] for info in layout.values())
    W = PAD_X * 2 + COL_W * n_cols
    H = PAD_TOP + PAD_BOT + LANE_H * (max_lane + 1)

    def x_of_col(c: int) -> float:
        return PAD_X + c * COL_W

    def node_box(info: dict) -> tuple[float, float, float, float]:
        s, e, lane = info["start_col"], info["end_col"], info["lane"]
        x = x_of_col(s) + 4
        w = max((e - s + 1) * COL_W - 8, MIN_W)
        # Clip width to stay inside chart.
        max_w = W - PAD_X - x
        w = min(w, max_w)
        y = PAD_TOP + lane * LANE_H + 6
        h = LANE_H - 10
        return x, y, w, h

    svg_parts: list[str] = []
    svg_parts.append(
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
        f'class="tree-svg" role="img" aria-label="Project tech tree">'
    )
    svg_parts.append('<defs>')
    svg_parts.append(
        '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#7c8599"/></marker>'
    )
    svg_parts.append(
        '<marker id="arrow-amber" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#e6a23c"/></marker>'
    )
    # Fade gradient for live nodes — fades nothing, but we use it for a glow.
    svg_parts.append('</defs>')

    # Background column grid.
    for i, m in enumerate(months):
        x = x_of_col(i)
        svg_parts.append(
            f'<line x1="{x}" x2="{x}" y1="{PAD_TOP - 6}" y2="{H - PAD_BOT / 2}" '
            f'class="tree-gridline"/>'
        )
        svg_parts.append(
            f'<text x="{x + COL_W / 2:.1f}" y="{PAD_TOP - 22}" '
            f'text-anchor="middle" class="tree-col-month">{fmt_month(m).split()[0]}</text>'
        )
        svg_parts.append(
            f'<text x="{x + COL_W / 2:.1f}" y="{PAD_TOP - 10}" '
            f'text-anchor="middle" class="tree-col-year">{fmt_month(m).split()[1]}</text>'
        )
    # HEAD column.
    x_head = x_of_col(n_cols - 1)
    svg_parts.append(
        f'<line x1="{x_head}" x2="{x_head}" y1="{PAD_TOP - 6}" y2="{H - PAD_BOT / 2}" '
        f'class="tree-gridline head"/>'
    )
    svg_parts.append(
        f'<text x="{x_head + COL_W / 2:.1f}" y="{PAD_TOP - 16}" '
        f'text-anchor="middle" class="tree-col-head">HEAD</text>'
    )

    # Edges (drawn first so nodes overlay them).
    for e in edges:
        src_info = layout.get(e["src"])
        dst_info = layout.get(e["dst"])
        if not src_info or not dst_info:
            continue
        sx, sy, sw, sh = node_box(src_info)
        dx, dy, dw, dh = node_box(dst_info)
        x1, y1 = sx + sw, sy + sh / 2
        x2, y2 = dx, dy + dh / 2
        # Smooth S-curve control points.
        cx1 = x1 + max((x2 - x1) * 0.5, 18)
        cx2 = x2 - max((x2 - x1) * 0.5, 18)
        path = f"M {x1:.1f} {y1:.1f} C {cx1:.1f} {y1:.1f}, {cx2:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}"
        kind = e["kind"]
        cls = f"tree-edge edge-{kind.replace('-', '')}"
        svg_parts.append(
            f'<path d="{path}" class="{cls}" '
            f'data-tip="{html.escape(kind)}: {html.escape(e["src"])} → {html.escape(e["dst"])}"/>'
        )

    # Nodes.
    for nid, info in layout.items():
        n = info["node"]
        x, y, w, h = node_box(info)
        status = n["status"]
        cat = n.get("category") or "ops"
        cat_color = CATEGORY_COLORS.get(cat, "#64748b")

        # Live nodes get a subtle right-edge continuation + arrow.
        if status == "live":
            node_cls = "tree-node live"
        elif status == "superseded":
            node_cls = "tree-node superseded"
        else:
            node_cls = "tree-node dead"

        # Capsule
        radius = h / 2
        svg_parts.append(
            f'<g class="{node_cls}" data-tip="{html.escape(n["label"])} · '
            f'{html.escape(status)} · {html.escape(n.get("description", ""))}">'
        )
        svg_parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="{radius:.1f}" class="tree-node-bg"/>'
        )
        # Left category tab.
        svg_parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="4" height="{h:.1f}" '
            f'fill="{cat_color}" rx="2"/>'
        )
        # Dead × marker at right edge.
        if status == "dead":
            xm = x + w + 4
            ym = y + h / 2
            svg_parts.append(
                f'<g class="tree-dead-mark" transform="translate({xm:.1f}, {ym:.1f})">'
                f'<line x1="-4" y1="-4" x2="4" y2="4"/>'
                f'<line x1="4" y1="-4" x2="-4" y2="4"/>'
                f'</g>'
            )
        # Label.
        label = n["label"]
        if len(label) > 34:
            label = label[:33] + "…"
        svg_parts.append(
            f'<text x="{x + 10:.1f}" y="{y + h / 2 + 4:.1f}" '
            f'class="tree-node-label">{html.escape(label)}</text>'
        )
        svg_parts.append('</g>')

    # Category legend.
    legend_items = sorted(CATEGORY_COLORS.items(),
                          key=lambda kv: kv[0])
    svg_parts.append('</svg>')

    legend = '<div class="tree-legend-row">'
    # Status legend
    legend += (
        '<span class="tree-leg-group">'
        '<span class="tree-leg status-live">● live</span>'
        '<span class="tree-leg status-sup">● superseded</span>'
        '<span class="tree-leg status-dead">× dead</span>'
        '</span>'
    )
    legend += '<span class="tree-leg-group">'
    for cat, color in legend_items:
        legend += (
            f'<span class="tree-leg cat"><i style="background:{color}"></i>{cat}</span>'
        )
    legend += '</span>'
    # Edge-kind legend
    legend += (
        '<span class="tree-leg-group">'
        '<span class="tree-leg"><svg width="22" height="8"><path d="M 0 4 C 7 0, 14 8, 22 4" class="tree-edge edge-evolvedinto" fill="none"/></svg> evolved-into</span>'
        '<span class="tree-leg"><svg width="22" height="8"><path d="M 0 4 C 7 0, 14 8, 22 4" class="tree-edge edge-replacedby" fill="none"/></svg> replaced-by</span>'
        '<span class="tree-leg"><svg width="22" height="8"><path d="M 0 4 C 7 0, 14 8, 22 4" class="tree-edge edge-enabled" fill="none"/></svg> enabled</span>'
        '<span class="tree-leg"><svg width="22" height="8"><path d="M 0 4 C 7 0, 14 8, 22 4" class="tree-edge edge-branchedfrom" fill="none"/></svg> branched-from</span>'
        '</span>'
    )
    legend += '</div>'

    return (
        f'<div class="tree-wrap">'
        f'{"".join(svg_parts)}'
        f'</div>'
        f'{legend}'
    )


def render_kind_stack(kinds: dict[str, int]) -> str:
    """A thin horizontal stacked bar of commit kinds for a month."""
    if not kinds:
        return ""
    total = sum(kinds.values())
    palette = {
        "feature": "#5bc784",
        "fix": "#e6a23c",
        "refactor": "#a78bfa",
        "infra": "#4da3ff",
        "docs": "#9ca3af",
        "chore": "#6b7280",
        "test": "#14b8a6",
        "style": "#ec4899",
        "merge": "#64748b",
        "unclear": "#52525b",
    }
    parts = []
    for kind, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
        pct = n / total * 100
        color = palette.get(kind, "#52525b")
        parts.append(
            f'<span class="kind-seg" style="width:{pct:.1f}%;background:{color}" '
            f'title="{kind}: {n}"></span>'
        )
    legend = " ".join(
        f'<span class="kind-leg"><i style="background:{palette.get(k, "#52525b")}"></i>'
        f'{k} <b>{v}</b></span>'
        for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])
    )
    return f'<div class="kind-bar">{"".join(parts)}</div><div class="kind-legend">{legend}</div>'


# ---------------------------------------------------------------------------
# Page templates.
# ---------------------------------------------------------------------------

BASE_CSS = """
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
:root {
  --bg: #0b0d12;
  --bg-elev: #141822;
  --bg-card: #181d29;
  --fg: #e4e6ec;
  --fg-dim: #99a0b1;
  --fg-mute: #646b7e;
  --border: #232836;
  --border-soft: #1c202c;
  --accent: var(--hue, 200);
  --green: #5bc784;
  --amber: #e6a23c;
  --red: #ef6e6e;
  --radius: 10px;
  --serif: "Spectral", "Iowan Old Style", Georgia, serif;
  --sans: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
}
body {
  background: var(--bg);
  color: var(--fg);
  font-family: var(--sans);
  font-size: 15px;
  line-height: 1.55;
  letter-spacing: -0.005em;
  -webkit-font-smoothing: antialiased;
}
a { color: hsl(var(--accent) 70% 72%); text-decoration: none; }
a:hover { text-decoration: underline; }
code { font-family: var(--mono); font-size: 0.9em;
  background: var(--bg-elev); padding: 0.12em 0.4em; border-radius: 4px; }
h1, h2, h3 { font-family: var(--serif); font-weight: 500; letter-spacing: -0.015em;
  line-height: 1.2; margin: 0 0 0.4em; }
h1 { font-size: 2.4rem; }
h2 { font-size: 1.4rem; margin-top: 2rem; }
h3 { font-size: 1.1rem; margin-top: 1.2rem; }

.container { max-width: 1080px; margin: 0 auto; padding: 0 24px; }

/* ===== hero ===== */
.hero {
  padding: 56px 24px 32px;
  border-bottom: 1px solid var(--border-soft);
  background: radial-gradient(ellipse at 10% 0%,
    hsl(var(--accent) 55% 20% / 0.35), transparent 70%);
}
.hero-top { display: flex; align-items: baseline; justify-content: space-between; gap: 24px; flex-wrap: wrap; }
.hero-title { margin: 0; }
.hero-title .dot {
  display: inline-block; width: 10px; height: 10px; border-radius: 50%;
  background: hsl(var(--accent) 70% 55%); margin-right: 12px; vertical-align: middle;
}
.hero-sub { color: var(--fg-dim); margin: 8px 0 0; font-size: 1.05rem; max-width: 60ch; }
.crumbs { font-size: 0.88rem; color: var(--fg-mute); }
.crumbs a { color: var(--fg-dim); }
.stat-strip {
  display: flex; flex-wrap: wrap; gap: 28px; margin-top: 28px;
  font-family: var(--mono);
}
.stat { display: flex; flex-direction: column; }
.stat .top { display: flex; align-items: center; gap: 7px; }
.stat .top svg { width: 15px; height: 15px; color: var(--fg-mute); flex: none; }
.stat .n { font-size: 1.4rem; color: var(--fg); font-variant-numeric: tabular-nums; line-height: 1.15; }
.stat .l { font-size: 0.72rem; color: var(--fg-mute); text-transform: uppercase; letter-spacing: 0.08em; margin-top: 4px; }

/* clustered hero strip: two semantic groups split by a divider */
.stat-strip.clustered { gap: 0; align-items: center; }
.stat-group { display: flex; flex-direction: column; gap: 12px; }
.stat-group .sg-label {
  font-size: 0.64rem; letter-spacing: 0.18em; text-transform: uppercase;
  color: var(--fg-mute); opacity: 0.75;
}
.stat-group .sg-items { display: flex; flex-wrap: wrap; gap: 30px; }
.stat-div { align-self: stretch; width: 1px; background: var(--border); margin: 0 34px; }
.stat-hero .n { color: hsl(var(--accent) 75% 72%); }
.stat-hero .ring { width: 22px; height: 22px; }
@media (max-width: 720px) {
  .stat-strip.clustered { gap: 22px; align-items: flex-start; flex-direction: column; }
  .stat-div { display: none; }
}

.watch-btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 9px 16px; border-radius: 999px;
  background: linear-gradient(135deg, hsl(var(--accent) 70% 45%), hsl(calc(var(--accent) + 40) 70% 55%));
  color: #fff !important; text-decoration: none; font-weight: 500; font-size: 0.88rem;
  letter-spacing: 0.02em;
  box-shadow: 0 4px 16px hsl(var(--accent) 70% 30% / 0.5);
  transition: transform 0.15s, box-shadow 0.15s;
}
.watch-btn:hover { transform: translateY(-1px); box-shadow: 0 6px 20px hsl(var(--accent) 70% 40% / 0.6); text-decoration: none; }

.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 14px; }
.chip {
  display: inline-flex; align-items: center; gap: 6px;
  background: var(--bg-elev); border: 1px solid var(--border);
  color: var(--fg-dim); padding: 3px 10px; border-radius: 999px;
  font-size: 0.82rem; font-family: var(--mono);
}
.chip.success { color: var(--green); border-color: hsl(145 40% 25%); background: hsl(145 30% 10%); }
.chip.danger  { color: var(--red);   border-color: hsl(2 40% 25%);   background: hsl(2 30% 10%); }
.chip.warn    { color: var(--amber); border-color: hsl(38 40% 25%);  background: hsl(38 30% 10%); }

/* ===== timeline ===== */
.section { padding: 40px 0; }
.section + .section { border-top: 1px solid var(--border-soft); }
.section h2 { color: var(--fg); }
.section .lede { color: var(--fg-dim); max-width: 65ch; margin: -0.5em 0 1.5em; }

.timeline { width: 100%; height: auto; display: block; }
.timeline .gridline { stroke: var(--border); stroke-width: 1; stroke-dasharray: 2 3; }
.timeline .gridlabel { font-family: var(--mono); font-size: 10px; fill: var(--fg-mute); }
.timeline .monthlabel { font-family: var(--mono); font-size: 11px; fill: var(--fg-dim); }
.timeline .monthlabel.dim { fill: var(--fg-mute); }
.timeline .bar-hit { cursor: pointer; }
.timeline .bar-hit:hover ~ .bar-surv { opacity: 1; }

.survcurve { width: 100%; height: auto; display: block; max-width: 760px; margin-top: 10px; }
.survcurve .gridline { stroke: var(--border); stroke-width: 1; stroke-dasharray: 2 3; }
.survcurve .gridline.mid { stroke: var(--fg-mute); stroke-dasharray: none; opacity: 0.45; }
.survcurve .axlabel { font-family: var(--mono); font-size: 10px; fill: var(--fg-mute); }
.survcurve .axtitle { font-family: var(--mono); font-size: 10px; fill: var(--fg-mute); letter-spacing: 0.06em; }
.survcurve .hl-line { stroke: hsl(var(--accent) 75% 62%); stroke-dasharray: 3 3; stroke-width: 1; opacity: 0.7; }
.survcurve .hl-label { font-family: var(--mono); font-size: 11px; fill: hsl(var(--accent) 75% 72%); }
.legend { display: flex; gap: 16px; margin-top: 12px; flex-wrap: wrap; font-size: 0.85rem; color: var(--fg-dim); }
.legend .swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; vertical-align: middle; margin-right: 6px; }
.legend .swatch.dim { opacity: 0.35; }

/* ===== month grid ===== */
.months {
  display: flex; flex-direction: column; gap: 14px; margin-top: 8px;
}
.month-card {
  display: grid; grid-template-columns: 160px 1fr; gap: 20px;
  background: var(--bg-card); border: 1px solid var(--border-soft);
  border-radius: var(--radius); padding: 18px 22px;
}
.month-card .m-label {
  font-family: var(--mono); color: var(--fg);
  display: flex; flex-direction: column; gap: 6px;
}
.month-card .m-label .big { font-size: 1.1rem; }
.month-card .m-label .sub { color: var(--fg-mute); font-size: 0.78rem; }
.month-card .m-body { min-width: 0; }
.month-card .m-stats { display: flex; flex-wrap: wrap; gap: 18px; margin-bottom: 10px; font-family: var(--mono); font-size: 0.82rem; }
.month-card .m-stats .stat-inline { color: var(--fg-dim); }
.month-card .m-stats .stat-inline b { color: var(--fg); font-weight: 500; }
.month-card .m-theme { color: var(--fg); margin: 6px 0 12px; }
.month-card .m-sec { color: var(--fg-mute); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em; margin-top: 10px; margin-bottom: 6px; }

.verdict-strip { display: inline-flex; align-items: center; gap: 6px;
  padding: 2px 10px; border-radius: 999px; font-size: 0.75rem;
  font-family: var(--mono); letter-spacing: 0.04em; }
.verdict-strip.green { background: hsl(145 40% 16%); color: var(--green); }
.verdict-strip.amber { background: hsl(38 40% 16%); color: var(--amber); }
.verdict-strip.red   { background: hsl(2 40% 16%); color: var(--red); }
.verdict-strip.unknown { background: var(--bg-elev); color: var(--fg-mute); }

.kind-bar { display: flex; width: 100%; height: 6px; border-radius: 3px; overflow: hidden; background: var(--border-soft); }
.kind-seg { display: block; height: 100%; }
.kind-legend { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 8px; font-size: 0.75rem; color: var(--fg-dim); font-family: var(--mono); }
.kind-leg i { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }

/* ===== narrative (markdown) ===== */
.narrative { color: var(--fg); line-height: 1.7; }
.narrative h1 { display: none; }
.narrative h2 { color: var(--fg); margin-top: 2.2rem; }
.narrative p { margin: 0.6em 0; }
.narrative ul { padding-left: 1.4em; }
.narrative li { margin: 0.25em 0; }
.narrative strong { color: var(--fg); }
.table-wrap { overflow-x: auto; margin: 1em 0; }
.narrative table { border-collapse: collapse; width: 100%; font-size: 0.92rem; }
.narrative th, .narrative td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border-soft); }
.narrative th { color: var(--fg-dim); font-weight: 500; font-family: var(--mono); font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.06em; }
.narrative td:nth-child(3), .narrative td:nth-child(2) { font-family: var(--mono); font-variant-numeric: tabular-nums; color: var(--fg-dim); }

/* ===== footer ===== */
footer { padding: 32px 0 64px; color: var(--fg-mute); font-size: 0.82rem; text-align: center; }
footer code { background: var(--bg-card); }

/* ===== index page ===== */
.repo-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 18px; margin-top: 24px; }
.repo-card {
  background: var(--bg-card); border: 1px solid var(--border-soft);
  border-radius: var(--radius); padding: 20px 22px; text-decoration: none; color: inherit;
  transition: border-color 0.15s, transform 0.15s;
  display: flex; flex-direction: column; gap: 10px;
  position: relative; overflow: hidden;
}
.repo-card::before {
  content: ""; position: absolute; top: 0; left: 0; right: 0; height: 3px;
  background: hsl(var(--accent) 60% 55%);
}
.repo-card:hover { border-color: hsl(var(--accent) 50% 40%); transform: translateY(-2px); }
.repo-card h3 { margin: 0; font-family: var(--sans); font-weight: 600; font-size: 1.2rem; }
.repo-card .repo-purpose { color: var(--fg-dim); font-size: 0.92rem; line-height: 1.5; flex: 1; }
.repo-card .repo-stats { font-family: var(--mono); font-size: 0.78rem; color: var(--fg-mute); display: flex; gap: 14px; flex-wrap: wrap; }
.repo-card .repo-stats b { color: var(--fg-dim); font-weight: 500; }

/* ===== tech tree DAG ===== */
.tree-wrap { overflow-x: auto; padding: 12px 0 6px; margin: 0 -8px; }
.tree-svg { display: block; min-width: 900px; }
.tree-gridline { stroke: var(--border-soft); stroke-width: 1; stroke-dasharray: 2 4; }
.tree-gridline.head { stroke: hsl(var(--accent) 50% 40%); stroke-dasharray: none; stroke-width: 1.2; opacity: 0.6; }
.tree-col-month { font-family: var(--mono); font-size: 10px; fill: var(--fg-dim); letter-spacing: 0.04em; text-transform: uppercase; }
.tree-col-year { font-family: var(--mono); font-size: 9px; fill: var(--fg-mute); }
.tree-col-head { font-family: var(--mono); font-size: 10px; fill: hsl(var(--accent) 60% 65%); letter-spacing: 0.12em; }

.tree-node { cursor: default; }
.tree-node-bg { fill: var(--bg-elev); stroke: var(--border); stroke-width: 1; }
.tree-node.live .tree-node-bg { fill: hsl(145 30% 13%); stroke: hsl(145 45% 36%); }
.tree-node.superseded .tree-node-bg { fill: hsl(38 30% 13%); stroke: hsl(38 45% 36%); stroke-dasharray: 3 2; }
.tree-node.dead .tree-node-bg { fill: hsl(2 30% 13%); stroke: hsl(2 45% 40%); stroke-dasharray: 2 3; opacity: 0.85; }
.tree-node-label { font-family: var(--sans); font-size: 11px; fill: var(--fg); font-weight: 500; pointer-events: none; }
.tree-node.dead .tree-node-label { fill: #d4a3a3; }
.tree-node.superseded .tree-node-label { fill: #d9b879; }
.tree-node:hover .tree-node-bg { filter: brightness(1.25); }
.tree-dead-mark line { stroke: var(--red); stroke-width: 2; stroke-linecap: round; }

.tree-edge { fill: none; stroke: #7c8599; stroke-width: 1.4; opacity: 0.7; }
.edge-evolvedinto { stroke: #5bc784; }
.edge-replacedby { stroke: #e6a23c; stroke-dasharray: 4 3; }
.edge-enabled { stroke: #7c8599; stroke-dasharray: 1 3; stroke-width: 1; opacity: 0.55; }
.edge-branchedfrom { stroke: #a78bfa; stroke-width: 1.2; opacity: 0.7; }

.tree-legend-row { display: flex; flex-wrap: wrap; gap: 18px; margin-top: 10px; font-size: 0.78rem; color: var(--fg-dim); }
.tree-leg-group { display: inline-flex; flex-wrap: wrap; gap: 10px 14px; align-items: center; }
.tree-leg { display: inline-flex; align-items: center; gap: 5px; font-family: var(--mono); }
.tree-leg svg { vertical-align: middle; }
.tree-leg.cat i { display: inline-block; width: 8px; height: 8px; border-radius: 2px; }
.tree-leg.status-live { color: var(--green); }
.tree-leg.status-sup { color: var(--amber); }
.tree-leg.status-dead { color: var(--red); }

/* ===== tooltip ===== */
.tooltip {
  position: fixed; z-index: 50; pointer-events: none;
  background: var(--bg-elev); border: 1px solid var(--border);
  color: var(--fg); padding: 6px 10px; border-radius: 6px;
  font-size: 0.8rem; max-width: 280px; display: none;
  box-shadow: 0 10px 30px rgba(0,0,0,0.35);
}

@media (max-width: 640px) {
  .month-card { grid-template-columns: 1fr; }
  .hero-top { flex-direction: column; align-items: flex-start; }
}
"""

BASE_JS = """
(function(){
  var tip = document.createElement('div');
  tip.className = 'tooltip';
  document.body.appendChild(tip);
  document.querySelectorAll('[data-tip]').forEach(function(el){
    el.addEventListener('mouseenter', function(e){
      tip.textContent = el.getAttribute('data-tip');
      tip.style.display = 'block';
    });
    el.addEventListener('mousemove', function(e){
      tip.style.left = (e.clientX + 14) + 'px';
      tip.style.top  = (e.clientY + 14) + 'px';
    });
    el.addEventListener('mouseleave', function(){
      tip.style.display = 'none';
    });
  });
})();
"""


def render_repo_page(repo_name: str, data: dict) -> str:
    hue = repo_hue(repo_name)
    b = data["bootstrap"]
    totals = data["totals"]
    months = data["months"]

    purpose = b.get("purpose", "A git-timeline analysis.")
    stack = b.get("stack", [])
    main_features = b.get("main_features", [])
    tech_debt = b.get("tech_debt", [])

    first = (totals.get("first_commit") or "")[:10]
    last = (totals.get("last_commit") or "")[:10]

    # Stat strip
    # Current size of the codebase = lines authored across history still alive at
    # HEAD (every present line was inserted by some commit and survived).
    total_loc = sum((m["surviving_lines"] or 0) for m in months)
    # Global survival: of every line ever inserted, the share still alive at HEAD.
    # (Not the mean of monthly ratios — that over-weights tiny months.)
    total_ins = totals.get("total_ins") or 0
    global_surv = (total_loc / total_ins) if total_ins else None

    # Code half-life + survival curve (from the survival_curve stage, if run).
    try:
        surv_curve = json.loads(data["meta"].get("code_survival_curve") or "[]")
    except (json.JSONDecodeError, TypeError):
        surv_curve = []
    hl_raw = data["meta"].get("code_half_life_months") or ""
    half_life = float(hl_raw) if hl_raw else None
    if half_life is not None:
        half_life_disp = f"{half_life:.0f} mo" if half_life >= 1.5 else f"{half_life:.1f} mo"
    elif surv_curve:
        half_life_disp = f">{surv_curve[-1]['age']} mo"
    else:
        half_life_disp = None  # stage not run → stat hidden

    stat_strip = build_stat_strip(totals, total_loc, global_surv, half_life_disp, hue)
    survival_curve_svg = render_survival_curve_svg(surv_curve, hue, half_life)
    survival_curve_section = (
        '<section class="section"><div class="container">'
        '<h2>Code survival curve</h2>'
        '<p class="lede">How long a line of code actually lives: the share still present '
        'at HEAD <em>N months after it was written</em>, pooled across every month\'s '
        'cohort and weighted by size. Unlike a single survival % this isn\'t '
        'recency-biased — recent code only contributes to the early ages (it hasn\'t had '
        'time to die). The <strong>half-life</strong> is where the curve crosses 50%.</p>'
        f'{survival_curve_svg}</div></section>'
    ) if survival_curve_svg else ''

    # Timeline viz.
    timeline_svg = render_timeline_svg(months, hue)

    # Month cards.
    month_cards = []
    for m in months:
        v = verdict_for(m["survival_ratio"])
        verdict_label = {
            "green": "productive",
            "amber": "mixed",
            "red": "dead end",
            "unknown": "—",
        }[v]
        shipped_chips = "".join(
            f'<span class="chip success">✓ {html.escape(s)}</span>'
            for s in m["shipped"][:10]
        )
        abandoned_chips = "".join(
            f'<span class="chip danger">✗ {html.escape(s)}</span>'
            for s in m["abandoned"][:10]
        )
        tag_chips = "".join(
            f'<span class="chip">#{html.escape(t)}</span>'
            for t in m["tags"][:8]
        )
        kind_viz = render_kind_stack(m["kinds"])
        month_cards.append(f"""
        <article class="month-card" id="month-{m['month']}">
          <div class="m-label">
            <span class="big">{html.escape(fmt_month(m['month']))}</span>
            <span class="verdict-strip {v}">{verdict_label}</span>
            <span class="sub">{m['commits']} commits</span>
          </div>
          <div class="m-body">
            <div class="m-stats">
              <span class="stat-inline">survival <b>{fmt_pct(m['survival_ratio'])}</b></span>
              <span class="stat-inline">churn ratio <b>{m['churn_ratio']:.2f}</b></span>
              <span class="stat-inline">+{fmt_int(m['ins'])} / -{fmt_int(m['dels'])}</span>
              <span class="stat-inline">{m['files_remaining']} files still at HEAD</span>
            </div>
            {kind_viz}
            <p class="m-theme">{html.escape(m['theme'])}</p>
            {'<div class="m-sec">shipped</div><div class="chips">' + shipped_chips + '</div>' if shipped_chips else ''}
            {'<div class="m-sec">abandoned</div><div class="chips">' + abandoned_chips + '</div>' if abandoned_chips else ''}
            {'<div class="m-sec">tags</div><div class="chips">' + tag_chips + '</div>' if tag_chips else ''}
          </div>
        </article>
        """)

    narrative_html = md_to_html(data["synthesis_md"]) if data["synthesis_md"] else ""
    tree_html = data.get("tree_html", "")

    stack_chips = "".join(
        f'<span class="chip">{html.escape(s)}</span>' for s in stack
    )
    feature_chips = "".join(
        f'<span class="chip">{html.escape(s)}</span>' for s in main_features
    )
    debt_chips = "".join(
        f'<span class="chip warn">{html.escape(s)}</span>' for s in tech_debt
    )

    # Spend table.
    spend_rows = "".join(
        f"<tr><td>{html.escape(r['stage'])}</td>"
        f"<td><code>{html.escape(r['model'])}</code></td>"
        f"<td>{r['n']}</td>"
        f"<td>{fmt_int(r['in_t'])}</td>"
        f"<td>{fmt_int(r['out_t'])}</td></tr>"
        for r in data["spend_rows"]
    )

    title = f"{repo_name} — hindsight timeline"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Spectral:wght@400;500&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{BASE_CSS}</style>
<style>:root {{ --hue: {hue}; }}</style>
</head>
<body>
<header class="hero">
  <div class="container">
    <div class="hero-top">
      <div>
        <div class="crumbs"><a href="../index.html">← all repos</a></div>
        <h1 class="hero-title"><span class="dot"></span>{html.escape(repo_name)}</h1>
        <p class="hero-sub">{html.escape(purpose)}</p>
      </div>
      <div style="text-align:right;display:flex;flex-direction:column;align-items:flex-end;gap:10px;">
        <div style="font-family:var(--mono);color:var(--fg-mute);font-size:0.78rem;letter-spacing:0.06em;text-transform:uppercase;">hindsight timeline</div>
        <div style="font-family:var(--mono);color:var(--fg-dim);font-size:0.82rem;">{first} → {last}</div>
        <a href="wrapped.html" class="watch-btn">▶ watch your wrapped</a>
      </div>
    </div>
    {stat_strip}
  </div>
</header>

<main>
  <section class="section">
    <div class="container">
      <h2>About</h2>
      <p class="lede">What this project is today, extracted from README, manifests, and file tree.</p>
      <h3>Stack</h3>
      <div class="chips">{stack_chips}</div>
      <h3>Main features</h3>
      <div class="chips">{feature_chips}</div>
      {'<h3>Tech debt signals</h3><div class="chips">' + debt_chips + '</div>' if debt_chips else ''}
    </div>
  </section>

  <section class="section">
    <div class="container">
      <h2>Timeline</h2>
      <p class="lede">Each bar is one month. Bar height is lines of code added (LoC) on a square-root scale, so one outsized month doesn't flatten the rest; filled portion is the share of that inserted code which still exists at HEAD. Color indicates verdict based on survival: <span style="color:var(--green)">productive</span>, <span style="color:var(--amber)">mixed</span>, <span style="color:var(--red)">dead end</span>.</p>
      {timeline_svg}
      <div class="legend">
        <span><i class="swatch" style="background:var(--green)"></i>survived → HEAD</span>
        <span><i class="swatch dim" style="background:var(--green)"></i>rewritten / removed</span>
        <span><i class="swatch" style="background:var(--amber)"></i>mixed (25–50% survival)</span>
        <span><i class="swatch" style="background:var(--red)"></i>dead end (&lt;25% survival)</span>
      </div>
    </div>
  </section>

  {survival_curve_section}

  {'<section class="section"><div class="container"><h2>Tech tree</h2><p class="lede">Technical threads (subsystems, framework choices, experiments) as a DAG. Horizontal position follows time; nodes span from when a thread appeared to when it was replaced or abandoned. Live threads extend to HEAD. Dead limbs end with ×. Evolution edges (green) show same-thread maturation; replacement edges (amber, dashed) show one tech supplanting another.</p>' + tree_html + '</div></section>' if tree_html else ''}

  <section class="section">
    <div class="container">
      <h2>Narrative</h2>
      <p class="lede">LLM-synthesized arc and dead-end analysis, grounded in monthly data.</p>
      <div class="narrative">{narrative_html}</div>
    </div>
  </section>

  <section class="section">
    <div class="container">
      <h2>Month detail</h2>
      <p class="lede">Every month with its theme, mechanical signals, and what the commit trail suggests shipped or was abandoned. Commit-kind mix is the thin color bar on each card.</p>
      <div class="months">{''.join(month_cards)}</div>
    </div>
  </section>

  <section class="section">
    <div class="container">
      <h2>Analysis cost</h2>
      <p class="lede">Tokens spent building this page, by stage. Reruns hit a content-hash cache and cost $0.</p>
      <div class="table-wrap">
        <table class="narrative">
          <thead><tr><th>stage</th><th>model</th><th>calls</th><th>input tokens</th><th>output tokens</th></tr></thead>
          <tbody>{spend_rows}</tbody>
        </table>
      </div>
    </div>
  </section>
</main>

<footer>
  Generated by <code>git-timeline</code> · {datetime.now().strftime('%Y-%m-%d %H:%M')}
</footer>
<script>{BASE_JS}</script>
</body>
</html>"""


def render_index_page(repos: list[dict]) -> str:
    cards = []
    for r in repos:
        hue = repo_hue(r["name"])
        b = r["data"].get("bootstrap", {})
        totals = r["data"].get("totals", {})
        purpose = b.get("purpose", "(no bootstrap summary yet)")
        stack = b.get("stack", [])
        first = (totals.get("first_commit") or "")[:10]
        last = (totals.get("last_commit") or "")[:10]
        cards.append(f"""
        <a class="repo-card" href="{html.escape(r['name'])}/index.html" style="--accent: {hue};">
          <h3>{html.escape(r['name'])}</h3>
          <p class="repo-purpose">{html.escape(purpose)}</p>
          <div class="chips">{''.join(f'<span class="chip">{html.escape(s)}</span>' for s in stack[:5])}</div>
          <div class="repo-stats">
            <span><b>{fmt_int(totals.get('commits'))}</b> commits</span>
            <span><b>{totals.get('months', 0)}</b> months</span>
            <span>{first} → {last}</span>
          </div>
        </a>""")

    empty_state = ""
    if not repos:
        empty_state = """
        <div style="padding:40px;background:var(--bg-card);border:1px dashed var(--border);border-radius:var(--radius);text-align:center;color:var(--fg-dim);">
          No analyzed repos yet. Run <code>python -m src.extract &lt;repo&gt;</code> to start.
        </div>"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>git-timeline — analyzed repos</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Spectral:wght@400;500&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{BASE_CSS}</style>
<style>:root {{ --hue: 220; }}</style>
</head>
<body>
<header class="hero">
  <div class="container">
    <div class="crumbs">git-timeline</div>
    <h1 class="hero-title"><span class="dot"></span>Hindsight timelines</h1>
    <p class="hero-sub">Each repo's git history, collapsed into a one-page retrospective of what shipped, what got rewritten, and which months moved the product forward.</p>
    <div class="stat-strip">
      <div class="stat"><span class="n">{len(repos)}</span><span class="l">repos analyzed</span></div>
      <div class="stat"><span class="n">{fmt_int(sum(r['data']['totals'].get('commits', 0) for r in repos))}</span><span class="l">commits indexed</span></div>
      <div class="stat"><span class="n">{sum(r['data']['totals'].get('months', 0) for r in repos)}</span><span class="l">months covered</span></div>
    </div>
  </div>
</header>

<main>
  <section class="section">
    <div class="container">
      <h2>Repos</h2>
      {empty_state}
      <div class="repo-grid">{''.join(cards)}</div>
    </div>
  </section>
</main>

<footer>
  Generated by <code>git-timeline</code> · {datetime.now().strftime('%Y-%m-%d %H:%M')}
</footer>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    cache_dir = args.cache or paths.cache_dir()
    out_dir = args.out or paths.site_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    dbs = sorted(cache_dir.glob("*.db"))
    print(f"scanning  : {cache_dir}")
    print(f"output    : {out_dir}")
    print(f"found     : {len(dbs)} dbs")

    rendered = []
    for db_path in dbs:
        name = db_path.stem
        conn = db_mod.open_db(db_path)
        data = load_repo_data(conn)
        conn.close()
        if data is None:
            print(f"  skip    : {name} (no commits extracted)")
            continue

        page = render_repo_page(name, data)
        repo_out = out_dir / name
        repo_out.mkdir(exist_ok=True)
        (repo_out / "index.html").write_text(page)
        # Wrapped experience.
        conn = db_mod.open_db(db_path)
        wdata = wrapped_mod.collect_wrapped_data(conn)
        conn.close()
        wrapped_html = wrapped_mod.render_wrapped_page(name, wdata, repo_hue(name))
        (repo_out / "wrapped.html").write_text(wrapped_html)
        rendered.append({"name": name, "data": data})
        print(f"  rendered: {name}/index.html ({len(page):,} bytes) + wrapped.html ({len(wrapped_html):,} bytes)")

    index = render_index_page(rendered)
    (out_dir / "index.html").write_text(index)
    print(f"  rendered: index.html ({len(index):,} bytes)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
