"""Git Wrapped — a Spotify-style, story-format retrospective of a repo.

Pulls data from the existing SQLite tables and emits a self-contained
`wrapped.html` per repo: full-viewport cards, tap-through navigation,
animated count-ups, with a narrative voice that reacts to the data.

Entry point:
    from .wrapped import render_wrapped_page
    html_str = render_wrapped_page(repo_name, conn, accent_hue)
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime


# ---------------------------------------------------------------------------
# Data gathering.
# ---------------------------------------------------------------------------

GENERATED_FILE_PATTERNS = (
    "types/api.d.ts",
    ".d.ts",
    "patches/",
    ".generated.",
    "assets/",
)


def _is_generated(path: str) -> bool:
    return any(p in path for p in GENERATED_FILE_PATTERNS)


def collect_wrapped_data(conn: sqlite3.Connection) -> dict:
    """Every query that feeds a card. One-shot, no LLM calls."""
    totals = conn.execute("""
        SELECT COUNT(*) commits,
               MIN(committed_at) first_commit,
               MAX(committed_at) last_commit,
               SUM(insertions) total_ins,
               SUM(deletions) total_dels,
               COUNT(DISTINCT author_name) authors,
               COUNT(DISTINCT month) months,
               COUNT(DISTINCT DATE(committed_at)) active_days
        FROM commits
    """).fetchone()

    total_surv = conn.execute(
        "SELECT COALESCE(SUM(surviving_lines), 0) s FROM month_survival"
    ).fetchone()["s"]

    # Cadence.
    first_dt = datetime.fromisoformat(totals["first_commit"].replace("Z", "+00:00")) if totals["first_commit"] else None
    last_dt = datetime.fromisoformat(totals["last_commit"].replace("Z", "+00:00")) if totals["last_commit"] else None
    span_days = max((last_dt - first_dt).days, 1) if first_dt and last_dt else 1

    # Busiest day.
    busy = conn.execute("""
        SELECT DATE(committed_at) d, COUNT(*) n
        FROM commits GROUP BY d ORDER BY n DESC, d ASC LIMIT 1
    """).fetchone()
    busy_subjects = []
    if busy:
        busy_subjects = [
            {"subject": r["subject"], "files": r["files_changed"]}
            for r in conn.execute("""
                SELECT subject, files_changed FROM commits
                WHERE DATE(committed_at) = ?
                ORDER BY (insertions + deletions) DESC LIMIT 5
            """, (busy["d"],))
        ]

    # Time-of-day + weekday patterns.
    hour_rows = list(conn.execute("""
        SELECT CAST(strftime('%H', committed_at) AS INTEGER) h, COUNT(*) n
        FROM commits GROUP BY h
    """))
    hours: dict[int, int] = {r["h"]: r["n"] for r in hour_rows}
    peak_hour = max(hours, key=hours.get) if hours else 0

    dow_rows = list(conn.execute("""
        SELECT CAST(strftime('%w', committed_at) AS INTEGER) d, COUNT(*) n
        FROM commits GROUP BY d
    """))
    dow: dict[int, int] = {r["d"]: r["n"] for r in dow_rows}
    dow_names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    peak_dow = max(dow, key=dow.get) if dow else 0

    total_commits = totals["commits"] or 0
    weekend_commits = dow.get(0, 0) + dow.get(6, 0)
    weekend_pct = (weekend_commits / total_commits) if total_commits else 0.0
    late_night = sum(hours.get(h, 0) for h in range(0, 5))
    late_night_pct = (late_night / total_commits) if total_commits else 0.0
    early_bird = sum(hours.get(h, 0) for h in range(5, 9))
    early_bird_pct = (early_bird / total_commits) if total_commits else 0.0

    # Cryptic commits: short subject, big churn, excluding common ones.
    cryptic = [
        {"subject": r["subject"], "files": r["files_changed"],
         "churn": r["insertions"] + r["deletions"],
         "date": r["committed_at"][:10], "sha": r["sha"][:8]}
        for r in conn.execute("""
            SELECT sha, committed_at, subject, files_changed, insertions, deletions
            FROM commits
            WHERE length(subject) <= 18
              AND files_changed >= 3
              AND subject NOT LIKE 'Merge %'
              AND subject NOT LIKE 'Revert %'
              AND subject != 'Initial commit'
            ORDER BY (insertions + deletions) DESC
            LIMIT 6
        """)
    ]

    # Peak month (highest commits with decent survival; fall back to max commits).
    peak_month_row = conn.execute("""
        SELECT c.month, COUNT(*) commits,
               SUM(c.insertions) ins,
               ms.theme, ms.shipped,
               sv.survival_ratio
        FROM commits c
        LEFT JOIN month_summaries ms ON ms.month = c.month
        LEFT JOIN month_survival sv ON sv.month = c.month
        GROUP BY c.month
        ORDER BY commits DESC, c.month ASC
        LIMIT 1
    """).fetchone()

    # Highest-survival month (different cut).
    best_survival_row = conn.execute("""
        SELECT ms.month, sv.survival_ratio, ms.theme,
               (SELECT COUNT(*) FROM commits c WHERE c.month = ms.month) commits
        FROM month_summaries ms
        JOIN month_survival sv ON sv.month = ms.month
        WHERE (SELECT COUNT(*) FROM commits c WHERE c.month = ms.month) >= 20
        ORDER BY sv.survival_ratio DESC LIMIT 1
    """).fetchone()

    # Biggest pivot: replaced-by edge where src had longest duration.
    # Compute a crude duration (start→end months) via tree_nodes.
    pivot = None
    pivots = list(conn.execute("""
        SELECT e.src, e.dst, e.kind,
               s.label src_label, s.start_month src_start, s.end_month src_end, s.description src_desc,
               d.label dst_label, d.start_month dst_start, d.description dst_desc
        FROM tree_edges e
        JOIN tree_nodes s ON s.id = e.src
        JOIN tree_nodes d ON d.id = e.dst
        WHERE e.kind = 'replaced-by'
    """))
    def months_between(a: str | None, b: str | None) -> int:
        if not a or not b:
            return 0
        try:
            ay, am = map(int, a.split("-"))
            by, bm = map(int, b.split("-"))
            return max((by - ay) * 12 + (bm - am), 0)
        except ValueError:
            return 0
    if pivots:
        pivot = max(
            pivots,
            key=lambda r: months_between(r["src_start"], r["src_end"] or r["dst_start"]),
        )
        pivot = {**dict(pivot),
                 "duration_months": months_between(pivot["src_start"],
                                                   pivot["src_end"] or pivot["dst_start"])}

    # Biggest dead end: longest-surviving dead node.
    dead = conn.execute("""
        SELECT id, label, start_month, end_month, description, category
        FROM tree_nodes
        WHERE status = 'dead'
    """).fetchall()
    biggest_dead = None
    if dead:
        scored = [
            (months_between(d["start_month"], d["end_month"]), dict(d))
            for d in dead
        ]
        scored.sort(key=lambda s: (-s[0], s[1]["start_month"] or ""))
        biggest_dead = scored[0][1]
        biggest_dead["duration_months"] = scored[0][0]

    # Load-bearing files (skip obvious generated).
    mvp_files = []
    for r in conn.execute(
        "SELECT path, surviving_lines, first_month FROM file_survival "
        "ORDER BY surviving_lines DESC LIMIT 40"
    ):
        if _is_generated(r["path"]):
            continue
        mvp_files.append(dict(r))
        if len(mvp_files) >= 6:
            break

    # Themes aggregated from monthly tags.
    tag_counts: Counter = Counter()
    for r in conn.execute("SELECT shipped FROM month_summaries"):
        try:
            blob = json.loads(r["shipped"] or "{}")
            for t in blob.get("tags", []):
                tag_counts[t] += 1
        except json.JSONDecodeError:
            pass
    top_themes = tag_counts.most_common(12)

    # Signal tags from commit_summaries (more granular).
    sig_counts: Counter = Counter()
    for r in conn.execute("SELECT signals FROM commit_summaries"):
        try:
            for s in json.loads(r["signals"] or "[]"):
                if s:
                    sig_counts[s] += 1
        except json.JSONDecodeError:
            pass
    top_signals = sig_counts.most_common(20)

    # Arc from synthesis.
    synth_row = conn.execute(
        "SELECT response FROM llm_cache WHERE stage = 'synthesize' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    arc_text = ""
    if synth_row:
        md = synth_row["response"]
        # Extract the **Arc:** line.
        m = re.search(r"\*\*Arc:\*\*\s*(.+?)(?:\n\n|\Z)", md, re.DOTALL)
        if m:
            arc_text = " ".join(m.group(1).split())

    # Bootstrap context (for intro/outro).
    bootstrap = {}
    br = conn.execute("SELECT value FROM meta WHERE key = 'bootstrap_json'").fetchone()
    if br:
        try:
            bootstrap = json.loads(br["value"])
        except json.JSONDecodeError:
            pass

    # Code half-life + survival curve (from the survival_curve stage, if run).
    hl_row = conn.execute(
        "SELECT value FROM meta WHERE key = 'code_half_life_months'").fetchone()
    half_life = float(hl_row["value"]) if hl_row and hl_row["value"] else None
    sc_row = conn.execute(
        "SELECT value FROM meta WHERE key = 'code_survival_curve'").fetchone()
    try:
        survival_curve = json.loads(sc_row["value"]) if sc_row and sc_row["value"] else []
    except (json.JSONDecodeError, TypeError):
        survival_curve = []

    # Per-month commits (for sparkline / fallbacks).
    month_series = [
        {"month": r["month"], "commits": r["n"]}
        for r in conn.execute(
            "SELECT month, COUNT(*) n FROM commits GROUP BY month ORDER BY month"
        )
    ]

    # Full per-month rows for the headline timeline chart (LoC + survival),
    # matching the shape render.render_timeline_svg expects.
    timeline_months = [
        {
            "month": r["month"],
            "commits": r["commits"],
            "ins": r["ins"] or 0,
            "survival_ratio": r["survival_ratio"],
            "theme": r["theme"] or "",
        }
        for r in conn.execute("""
            SELECT c.month, COUNT(*) commits, SUM(c.insertions) ins,
                   ms.theme, sv.survival_ratio
            FROM commits c
            LEFT JOIN month_summaries ms ON ms.month = c.month
            LEFT JOIN month_survival sv ON sv.month = c.month
            GROUP BY c.month
            ORDER BY c.month ASC
        """)
    ]

    # Kind distribution for "what you built".
    kinds = {
        r["kind"]: r["n"]
        for r in conn.execute(
            "SELECT kind, COUNT(*) n FROM commit_summaries GROUP BY kind ORDER BY n DESC"
        )
    }

    return {
        "totals": dict(totals),
        "total_surv": total_surv,
        "span_days": span_days,
        "commits_per_week": (total_commits / span_days * 7) if span_days else 0,
        "busy": dict(busy) if busy else None,
        "busy_subjects": busy_subjects,
        "hours": hours,
        "dow": dow,
        "peak_hour": peak_hour,
        "peak_dow": peak_dow,
        "peak_dow_name": dow_names[peak_dow] if peak_dow in range(7) else "?",
        "weekend_pct": weekend_pct,
        "late_night_pct": late_night_pct,
        "early_bird_pct": early_bird_pct,
        "cryptic": cryptic,
        "peak_month": dict(peak_month_row) if peak_month_row else None,
        "best_survival": dict(best_survival_row) if best_survival_row else None,
        "pivot": pivot,
        "biggest_dead": biggest_dead,
        "mvp_files": mvp_files,
        "top_themes": top_themes,
        "top_signals": top_signals,
        "arc": arc_text,
        "bootstrap": bootstrap,
        "month_series": month_series,
        "timeline_months": timeline_months,
        "half_life": half_life,
        "survival_curve": survival_curve,
        "kinds": kinds,
    }


# ---------------------------------------------------------------------------
# Copy helpers — narrative voice that reacts to the data.
# ---------------------------------------------------------------------------

def _hour_period(h: int) -> str:
    if h < 5:  return "the dead of night"
    if h < 9:  return "the early morning"
    if h < 12: return "late morning"
    if h < 14: return "midday"
    if h < 18: return "the afternoon"
    if h < 22: return "the evening"
    return "late night"


def _hour_12(h: int) -> str:
    ampm = "am" if h < 12 else "pm"
    hh = h % 12 or 12
    return f"{hh}{ampm}"


def _pace_flavor(per_week: float) -> str:
    if per_week >= 60: return "That's a commit every two hours. Every. Two. Hours."
    if per_week >= 30: return "Basically a commit every other hour of your waking life."
    if per_week >= 15: return "A respectable cadence. Your GitHub contribution graph is smug."
    if per_week >= 7:  return "More-than-daily. You're not phoning it in."
    if per_week >= 3:  return "A steady drip. Consistency is a superpower."
    return "Quality over quantity. Or so we tell ourselves."


def _early_bird_flavor(pct: float, peak_hour: int) -> str:
    if pct >= 0.4:
        return f"You're a sunrise developer. {int(pct * 100)}% of your commits landed before 9am."
    if pct >= 0.25:
        return f"Morning person detected. {int(pct * 100)}% of commits before 9am."
    return ""


def _late_night_flavor(pct: float) -> str:
    if pct >= 0.2:
        return f"{int(pct * 100)}% of commits landed between midnight and 5am. We're worried."
    if pct >= 0.1:
        return f"{int(pct * 100)}% of commits were past midnight. Not great, not terrible."
    return ""


def _cryptic_quip(subject: str) -> str:
    s = subject.lower().strip()
    if s in ("yolo", "wtf", "omg", "fml"):
        return "A cry for help, or a victory lap? We'll never know."
    if s == "bankruptcy":
        return "Declaring it, apparently."
    if s in ("fix", "update", "wip"):
        return "The classics. Timeless. Useless."
    if "finally" in s or "jfc" in s:
        return "Fourteen hours of debugging in one commit message."
    if "cp" == s:
        return "Copy-paste as a verb. Copy-paste as a lifestyle."
    return ""


def _halflife_quip(hl: float | None) -> str:
    if hl is None:
        return "Most of what you write is still standing. Built to last."
    if hl < 3:
        return "You rewrite everything. Half your code doesn't survive a season."
    if hl < 6:
        return "Heavy churn — your codebase barely recognizes itself from last quarter."
    if hl < 12:
        return "A healthy rate of reinvention. Half-life under a year."
    if hl < 24:
        return "Your code has staying power. It outlives most New Year's resolutions."
    return "Built to last — what you write tends to stick around for years."


def _dead_quip(label: str, duration: int) -> str:
    if duration == 0:
        return "Scaffolded and abandoned in the same month. Didn't even get a birthday."
    if duration <= 1:
        return f"It lived {duration} month. Less than a milk carton."
    if duration <= 3:
        return f"You gave it {duration} months. Then you stopped returning its calls."
    return f"It hung around for {duration} months before you finally pulled the plug."


# ---------------------------------------------------------------------------
# HTML generation.
# ---------------------------------------------------------------------------

WRAPPED_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; overflow: hidden; background: #000; color: #fff; font-family: "Inter", -apple-system, sans-serif; }
body { -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
button { font: inherit; color: inherit; border: none; background: transparent; cursor: pointer; }
a { color: inherit; }

.app {
  position: fixed; inset: 0;
  display: flex; align-items: center; justify-content: center;
  overflow: hidden;
}
.card {
  position: absolute; inset: 0;
  display: flex; flex-direction: column;
  padding: max(6vh, 32px) max(6vw, 28px) max(10vh, 80px);
  opacity: 0; pointer-events: none;
  transition: opacity 0.45s ease, transform 0.45s ease;
  transform: translateY(24px);
  overflow-y: auto;
}
.card.active { opacity: 1; pointer-events: auto; transform: translateY(0); }
.card.exit-left { transform: translateX(-10px); }
.card .big { font-family: "Fraunces", "Spectral", Georgia, serif; font-weight: 500; line-height: 1; letter-spacing: -0.03em; font-size: clamp(48px, 12vw, 160px); }
.card .kicker { font-family: "JetBrains Mono", monospace; text-transform: uppercase; letter-spacing: 0.18em; font-size: 0.78rem; opacity: 0.7; margin-bottom: 16px; }
.card .sub { font-size: clamp(18px, 2.2vw, 26px); max-width: 28ch; line-height: 1.35; margin-top: 20px; opacity: 0.92; }
.card .sub.tight { max-width: 34ch; }
.card .quip { font-style: italic; opacity: 0.75; margin-top: 14px; font-size: clamp(14px, 1.6vw, 18px); max-width: 40ch; }
.card .spacer { flex: 1; }
.card .footer-hint { font-family: "JetBrains Mono", monospace; font-size: 0.72rem; opacity: 0.5; letter-spacing: 0.12em; text-transform: uppercase; }

/* progress bar */
.progress {
  position: fixed; top: 14px; left: 14px; right: 14px; height: 3px;
  display: flex; gap: 3px; z-index: 10;
}
.progress span {
  flex: 1; background: rgba(255,255,255,0.22);
  border-radius: 2px; overflow: hidden; position: relative;
}
.progress span.done::after,
.progress span.current::after {
  content: ""; position: absolute; inset: 0;
  background: rgba(255,255,255,0.95);
  transform-origin: left;
}
.progress span.done::after { transform: scaleX(1); }
.progress span.current::after { animation: progfill 0.45s linear forwards; }
@keyframes progfill { from { transform: scaleX(0); } to { transform: scaleX(1); } }

.nav-zones { position: absolute; inset: 0; display: flex; z-index: 5; }
.nav-zones .zone { flex: 1; }
.nav-zones .zone:first-child { flex: 0.35; }

.topbar {
  position: fixed; top: 26px; left: 0; right: 0;
  display: flex; justify-content: space-between;
  padding: 0 24px; z-index: 11;
  font-family: "JetBrains Mono", monospace; font-size: 0.72rem;
  text-transform: uppercase; letter-spacing: 0.14em;
}
.topbar a, .topbar button { opacity: 0.7; }
.topbar a:hover, .topbar button:hover { opacity: 1; }

/* gradient backgrounds */
.bg { position: absolute; inset: 0; z-index: -1; overflow: hidden; }
.bg::before, .bg::after {
  content: ""; position: absolute; border-radius: 50%;
  filter: blur(90px); opacity: 0.7;
}

.bg-intro { background: linear-gradient(135deg, #1a0a36 0%, #4a148c 45%, #c2185b 100%); }
.bg-intro::before { width: 60vw; height: 60vw; top: -20vw; right: -15vw; background: #ff4081; opacity: 0.4; }

/* headline timeline: keep it mostly dark so the chart pops */
.bg-timeline { background: linear-gradient(160deg, #07101f 0%, #11203a 55%, #1d3a63 100%); }
.bg-timeline::before { width: 55vw; height: 55vw; top: -22vw; right: -18vw; background: #2f6db5; opacity: 0.28; }

.bg-commits { background: linear-gradient(160deg, #3e0c47 0%, #8f1e4f 55%, #ff5261 100%); }
.bg-commits::before { width: 50vw; height: 50vw; bottom: -15vw; left: -15vw; background: #ffb74d; opacity: 0.35; }

.bg-busy { background: linear-gradient(135deg, #1a1028 0%, #4e1a6b 50%, #ff6d39 100%); }
.bg-busy::before { width: 45vw; height: 45vw; top: -10vw; right: -10vw; background: #ffd54f; opacity: 0.45; }

.bg-cadence { background: linear-gradient(145deg, #062b3a 0%, #134e5e 50%, #71b280 100%); }
.bg-cadence::before { width: 40vw; height: 40vw; bottom: -12vw; right: -10vw; background: #ffeb3b; opacity: 0.3; }

.bg-cryptic { background: linear-gradient(135deg, #0a0f0a 0%, #1a2e1a 50%, #54ff8e 100%); color: #d4ffd8; }
.bg-cryptic .big { font-family: "JetBrains Mono", monospace; font-weight: 500; }
.bg-cryptic::before { width: 50vw; height: 50vw; top: 20vw; left: -15vw; background: #00e676; opacity: 0.25; }

.bg-peak { background: linear-gradient(140deg, #2e1a00 0%, #b8860b 60%, #ffd54f 100%); }
.bg-peak::before { width: 55vw; height: 55vw; top: -20vw; left: -15vw; background: #ff9800; opacity: 0.45; }

.bg-churn { background: linear-gradient(155deg, #1a0606 0%, #8b1a1a 60%, #ff6b6b 100%); }
.bg-churn::before { width: 50vw; height: 50vw; bottom: -15vw; left: -10vw; background: #ffd54f; opacity: 0.25; }

.bg-halflife { background: linear-gradient(150deg, #041a14 0%, #0c5b46 55%, #2fd2a0 100%); }
.bg-halflife::before { width: 48vw; height: 48vw; top: -16vw; right: -12vw; background: #34d399; opacity: 0.3; }

/* survival curve (reuses render.render_survival_curve_svg output) — dark variant */
.survcurve { width: 100%; height: auto; display: block; max-width: 540px; margin-top: 26px; }
.survcurve .gridline { stroke: rgba(255,255,255,0.16); stroke-width: 1; stroke-dasharray: 2 3; }
.survcurve .gridline.mid { stroke: rgba(255,255,255,0.42); stroke-dasharray: none; }
.survcurve .axlabel { font-family: "JetBrains Mono", monospace; font-size: 10px; fill: rgba(255,255,255,0.6); }
.survcurve .axtitle { font-family: "JetBrains Mono", monospace; font-size: 10px; fill: rgba(255,255,255,0.6); }
.survcurve .hl-line { stroke: rgba(255,255,255,0.65); stroke-dasharray: 3 3; stroke-width: 1; }
.survcurve .hl-label { font-family: "JetBrains Mono", monospace; font-size: 11px; fill: #fff; }

.bg-pivot { background: linear-gradient(145deg, #0a0a2e 0%, #1e3a8a 55%, #06b6d4 100%); }
.bg-pivot::before { width: 45vw; height: 45vw; top: -10vw; right: -10vw; background: #7c3aed; opacity: 0.4; }

.bg-dead { background: linear-gradient(145deg, #141018 0%, #2d1a2e 50%, #6b4d6b 100%); color: #d4c8d4; }
.bg-dead::before { width: 40vw; height: 40vw; bottom: -10vw; right: -10vw; background: #4a2d4a; opacity: 0.5; }

.bg-mvp { background: linear-gradient(140deg, #0a1428 0%, #1e3a8a 60%, #3b82f6 100%); }
.bg-mvp::before { width: 50vw; height: 50vw; top: -15vw; right: -10vw; background: #60a5fa; opacity: 0.35; }

.bg-themes { background: linear-gradient(145deg, #042a2b 0%, #0e6b69 55%, #4ecdc4 100%); }
.bg-themes::before { width: 45vw; height: 45vw; top: 10vw; right: -15vw; background: #ff6b6b; opacity: 0.25; }

.bg-arc { background: linear-gradient(160deg, #030712 0%, #1e1b4b 50%, #6366f1 100%); }
.bg-arc::before { width: 50vw; height: 50vw; bottom: -10vw; right: -5vw; background: #a78bfa; opacity: 0.3; }

.bg-outro { background: radial-gradient(ellipse at 50% 50%, #1a0a2e 0%, #000 80%); }

/* element styles within cards */
.stat-row { display: flex; gap: 32px; margin-top: 26px; flex-wrap: wrap; }
.stat-row .s { }
.stat-row .s .n { font-family: "JetBrains Mono", monospace; font-size: clamp(22px, 3vw, 38px); line-height: 1; }
.stat-row .s .l { font-size: 0.72rem; opacity: 0.7; text-transform: uppercase; letter-spacing: 0.12em; margin-top: 6px; font-family: "JetBrains Mono", monospace; }

.list { list-style: none; padding: 0; margin-top: 22px; display: flex; flex-direction: column; gap: 12px; }
.list li { font-size: clamp(15px, 1.8vw, 19px); line-height: 1.45; }
.list li .rank { font-family: "JetBrains Mono", monospace; opacity: 0.6; margin-right: 14px; display: inline-block; width: 2ch; text-align: right; }
.list li .subj { font-family: "JetBrains Mono", monospace; background: rgba(255,255,255,0.1); padding: 2px 8px; border-radius: 4px; }
.list li .meta { opacity: 0.6; font-size: 0.85em; margin-left: 8px; }

.tag-cloud { display: flex; flex-wrap: wrap; gap: 8px 10px; margin-top: 28px; max-width: 42ch; }
.tag-cloud .tag { display: inline-block; padding: 4px 14px; border-radius: 999px; background: rgba(255,255,255,0.12); font-family: "JetBrains Mono", monospace; font-size: 14px; }
.tag-cloud .tag.big { font-size: 22px; padding: 6px 18px; background: rgba(255,255,255,0.22); }
.tag-cloud .tag.huge { font-size: 32px; padding: 8px 22px; background: rgba(255,255,255,0.28); font-weight: 500; }

.card .arc-text { font-family: "Fraunces", Georgia, serif; font-size: clamp(20px, 2.4vw, 30px); line-height: 1.42; font-weight: 400; max-width: 32ch; margin-top: 32px; }

.sparkline { display: flex; align-items: flex-end; gap: 3px; height: 80px; margin-top: 30px; max-width: 500px; }
.sparkline i { flex: 1; background: rgba(255,255,255,0.85); border-radius: 2px 2px 0 0; min-height: 3px; }

/* headline timeline chart (reuses render.render_timeline_svg output) */
.timeline { width: 100%; height: auto; display: block; margin-top: 22px; max-width: 920px; }
.timeline .gridline { stroke: rgba(255,255,255,0.16); stroke-width: 1; stroke-dasharray: 2 3; }
.timeline .gridlabel { font-family: "JetBrains Mono", monospace; font-size: 10px; fill: rgba(255,255,255,0.55); }
.timeline .monthlabel { font-family: "JetBrains Mono", monospace; font-size: 11px; fill: rgba(255,255,255,0.82); }
.timeline .monthlabel.dim { fill: rgba(255,255,255,0.5); }
.timeline .bar-hit { pointer-events: none; }  /* taps fall through to nav zones */
.tl-legend { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-top: 16px; font-family: "JetBrains Mono", monospace; font-size: 0.72rem; opacity: 0.85; }
.tl-legend span { display: inline-flex; align-items: center; gap: 7px; }
.tl-legend i { width: 11px; height: 11px; border-radius: 3px; display: inline-block; }
.tl-legend i.surv { background: #5bc784; }
.tl-legend i.lost { background: #5bc784; opacity: 0.35; }
.tl-legend i.dead { background: #ef6e6e; }

.outro-title { font-family: "Fraunces", Georgia, serif; font-size: clamp(72px, 14vw, 180px); line-height: 0.95; letter-spacing: -0.04em; }
.share-hint { margin-top: 24px; opacity: 0.7; font-size: 0.9rem; }

@media (max-width: 640px) {
  .stat-row { gap: 18px; }
}
"""


WRAPPED_JS_TEMPLATE = r"""
(function(){
  const cards = Array.from(document.querySelectorAll('.card'));
  const progressSegs = Array.from(document.querySelectorAll('.progress span'));
  let idx = 0;

  function activate(i, dir) {
    if (i < 0) i = 0;
    if (i >= cards.length) i = cards.length - 1;
    cards.forEach((c, j) => {
      c.classList.toggle('active', j === i);
      c.classList.toggle('exit-left', j < i);
    });
    progressSegs.forEach((s, j) => {
      s.classList.toggle('done', j < i);
      s.classList.toggle('current', j === i);
    });
    if (i !== idx) {
      const active = cards[i];
      active.querySelectorAll('[data-countup]').forEach(el => {
        const target = parseFloat(el.dataset.countup);
        countUp(el, target, 1200);
      });
    }
    idx = i;
  }

  function countUp(el, target, duration) {
    const start = performance.now();
    const isFloat = target % 1 !== 0;
    const fmt = (n) => {
      if (isFloat) return n.toFixed(1);
      return Math.floor(n).toLocaleString();
    };
    const tick = (now) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      el.textContent = fmt(target * eased);
      if (t < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  function next() { activate(Math.min(idx + 1, cards.length - 1)); }
  function prev() { activate(Math.max(idx - 1, 0)); }

  document.addEventListener('keydown', e => {
    if (e.key === 'ArrowRight' || e.key === ' ') { e.preventDefault(); next(); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); prev(); }
    else if (e.key === 'Escape') { window.location.href = 'index.html'; }
    else if (e.key === 'Home') activate(0);
    else if (e.key === 'End') activate(cards.length - 1);
  });

  document.querySelector('.zone.prev').addEventListener('click', prev);
  document.querySelector('.zone.next').addEventListener('click', next);

  // Touch swipe
  let touchStart = null;
  document.addEventListener('touchstart', e => { touchStart = e.touches[0].clientX; });
  document.addEventListener('touchend', e => {
    if (touchStart === null) return;
    const dx = (e.changedTouches[0].clientX - touchStart);
    if (Math.abs(dx) > 40) { dx < 0 ? next() : prev(); }
    touchStart = null;
  });

  // Initial kick-off: tick count-ups on first card.
  setTimeout(() => activate(0), 50);
})();
"""


def _esc(s: str) -> str:
    return html.escape(s if s is not None else "")


def _card(bg_class: str, kicker: str, body_html: str, hint: str = "") -> str:
    hint_html = f'<div class="footer-hint">{_esc(hint)}</div>' if hint else ""
    return (
        f'<section class="card">'
        f'<div class="bg {bg_class}"></div>'
        f'<div class="kicker">{_esc(kicker)}</div>'
        f'{body_html}'
        f'<div class="spacer"></div>'
        f'{hint_html}'
        f'</section>'
    )


def _countup(n: int | float, *, as_float: bool = False) -> str:
    val = f"{n:.1f}" if as_float else f"{int(n):,}"
    return f'<span data-countup="{n}">{val}</span>'


def render_wrapped_page(repo_name: str, data: dict, hue: int = 210) -> str:
    # Lazy import to avoid a circular import (render imports wrapped at top).
    from .render import render_timeline_svg, render_survival_curve_svg

    t = data["totals"]
    first = (t.get("first_commit") or "")[:10]
    last = (t.get("last_commit") or "")[:10]
    commits = t.get("commits") or 0
    span_months = t.get("months") or 0
    total_ins = t.get("total_ins") or 0
    surv_pct = int(round((data["total_surv"] / total_ins) * 100)) if total_ins else 0

    # Cards list.
    cards: list[str] = []

    # 1. HEADLINE: the timeline chart. Show the whole arc first, then explore it.
    timeline_svg = (
        render_timeline_svg(data["timeline_months"], hue)
        if data.get("timeline_months") else ""
    )
    legend = (
        '<div class="tl-legend">'
        '<span><i class="surv"></i>still alive at HEAD</span>'
        '<span><i class="lost"></i>later rewritten</span>'
        '<span><i class="dead"></i>dead-end month</span>'
        '</div>'
    )
    cards.append(_card(
        "bg-timeline",
        f"{repo_name} · wrapped",
        f'<div class="big" style="font-size:clamp(34px,7vw,76px);">{span_months} months,<br>one chart</div>'
        f'{timeline_svg}'
        f'{legend}'
        f'<div class="sub tight" style="margin-top:18px;">Each bar is a month — '
        f'height is code written (√-scaled), the bright fill is what\'s still '
        f'alive today. {_countup(commits)} commits, {surv_pct}% still standing.</div>',
        "tap or press → to explore it, month by month"
    ))

    # 2. Code half-life (only if the survival_curve stage has run).
    sc = data.get("survival_curve") or []
    if len(sc) >= 2:
        hl = data.get("half_life")
        if hl is not None:
            hl_big = f"{round(hl)}"
            hl_line = "months until half the code you write is gone."
        else:
            hl_big = f">{sc[-1]['age']}"
            hl_line = "months in — and over half your code is still alive."
        curve_svg = render_survival_curve_svg(sc, hue, hl)
        cards.append(_card(
            "bg-halflife",
            "your code's half-life",
            f'<div class="big" style="font-size:clamp(64px,15vw,180px);">{hl_big}</div>'
            f'<div class="sub" style="margin-top:6px;">{hl_line}</div>'
            f'{curve_svg}'
            f'<div class="quip">{_esc(_halflife_quip(hl))}</div>',
            "every line you write is on the clock"
        ))

    # 3. Total commits + cadence (sparkline dropped — the headline chart above
    #    already shows monthly volume, in richer form).
    per_week = data["commits_per_week"]
    pace_flav = _pace_flavor(per_week)
    cards.append(_card(
        "bg-commits",
        "commits shipped",
        f'<div class="big">{_countup(commits)}</div>'
        f'<div class="sub">That\'s {_countup(per_week, as_float=True)} commits per week, averaged across the whole run.</div>'
        f'<div class="quip">{_esc(pace_flav)}</div>',
        f"{t.get('active_days') or 0} active days of the last {data['span_days']}"
    ))

    # 3. Busiest day.
    busy = data["busy"]
    if busy:
        subs = data["busy_subjects"][:4]
        subs_html = "<ul class='list'>" + "".join(
            f'<li><span class="rank">·</span><span class="subj">{_esc(s["subject"][:70])}</span>'
            f'<span class="meta">{s["files"]}f</span></li>'
            for s in subs
        ) + "</ul>"
        cards.append(_card(
            "bg-busy",
            "your biggest day",
            f'<div class="kicker" style="margin-top:-4px;opacity:0.85;">{busy["d"]}</div>'
            f'<div class="big">{_countup(busy["n"])}</div>'
            f'<div class="sub">commits in one day. This is what your energy looked like:</div>'
            f'{subs_html}',
            "whatever happened, you were locked in"
        ))

    # 4. Cadence (when you like to commit).
    early = _early_bird_flavor(data["early_bird_pct"], data["peak_hour"])
    late = _late_night_flavor(data["late_night_pct"])
    weekend = int(data["weekend_pct"] * 100)
    flav_parts = [f for f in (early, late) if f]
    if not flav_parts:
        flav_parts = [f"Peak hour: {_hour_12(data['peak_hour'])}."]
    cards.append(_card(
        "bg-cadence",
        "your coding rhythm",
        f'<div class="big" style="font-size:clamp(40px,9vw,110px);">{_esc(data["peak_dow_name"])}</div>'
        f'<div class="sub">is your favorite day to commit. '
        f'You peak at {_hour_12(data["peak_hour"])} — {_esc(_hour_period(data["peak_hour"]))}.</div>'
        f'<div class="quip">' + " ".join(_esc(f) for f in flav_parts) + f' {weekend}% of commits fall on weekends.</div>',
        "know thyself"
    ))

    # 5. Cryptic commits.
    if data["cryptic"]:
        top = data["cryptic"][:4]
        quip_text = _cryptic_quip(top[0]["subject"]) if top else ""
        li_html = "".join(
            f'<li><span class="rank">{i+1}</span><span class="subj">{_esc(c["subject"])}</span>'
            f'<span class="meta">{c["files"]} files · {c["churn"]:,} lines · {c["date"]}</span></li>'
            for i, c in enumerate(top)
        )
        cards.append(_card(
            "bg-cryptic",
            "your most cryptic commits",
            f'<div class="big" style="font-size:clamp(40px,8vw,80px);">hall of<br>fame</div>'
            f'<div class="sub">Short subjects. Big changes. No context.</div>'
            f'<ul class="list">{li_html}</ul>'
            f'<div class="quip">{_esc(quip_text)}</div>',
            "tap → to learn what actually happened"
        ))

    # 6. Peak month.
    pm = data["peak_month"]
    if pm:
        theme = pm.get("theme") or ""
        surv_s = pm.get("survival_ratio")
        surv_html = f'{int(surv_s*100)}% still alive today' if surv_s is not None else ""
        cards.append(_card(
            "bg-peak",
            "your peak month",
            f'<div class="big" style="font-size:clamp(40px,9vw,120px);">{_esc(_fmt_month_name(pm["month"]))}</div>'
            f'<div class="sub tight">{_countup(pm["commits"])} commits — more than any other month.'
            f'{f" {_esc(surv_html)}." if surv_html else ""}</div>'
            f'{f"<div class=" + chr(34) + "quip" + chr(34) + ">" + _esc(theme) + "</div>" if theme else ""}',
            ""
        ))

    # 7. Churn reality.
    total_ins = t.get("total_ins") or 0
    total_dels = t.get("total_dels") or 0
    surv = data["total_surv"]
    surv_pct = (surv / total_ins) if total_ins else 0.0
    cards.append(_card(
        "bg-churn",
        "the churn reality",
        f'<div class="big">{_countup(total_ins)}</div>'
        f'<div class="sub">lines written. {_countup(surv)} still exist at HEAD.</div>'
        f'<div class="quip">That\'s {int(surv_pct*100)}% survival. '
        f'The rest? Demolition, rework, second thoughts — and the occasional "bankruptcy".</div>'
        f'<div class="stat-row" style="margin-top:30px;">'
        f'  <div class="s"><div class="n">+{_countup(total_ins)}</div><div class="l">inserted</div></div>'
        f'  <div class="s"><div class="n">-{_countup(total_dels)}</div><div class="l">deleted</div></div>'
        f'  <div class="s"><div class="n">{int(surv_pct*100)}%</div><div class="l">survived</div></div>'
        f'</div>',
        ""
    ))

    # 8. Biggest pivot.
    p = data["pivot"]
    if p:
        cards.append(_card(
            "bg-pivot",
            "the great pivot",
            f'<div class="big" style="font-size:clamp(36px,7vw,84px);line-height:1.05;">'
            f'{_esc(p["src_label"])} <span style="opacity:0.45;">→</span><br>{_esc(p["dst_label"])}</div>'
            f'<div class="sub tight">After {p["duration_months"]} month{"s" if p["duration_months"] != 1 else ""}, '
            f'you traded one for the other.</div>'
            f'<div class="quip">{_esc(p["src_desc"] or "")}</div>',
            f"{p['src_start']} → {p['dst_start']}"
        ))

    # 9. Biggest dead end.
    d = data["biggest_dead"]
    if d:
        quip = _dead_quip(d["label"], d["duration_months"])
        cards.append(_card(
            "bg-dead",
            "rest in peace",
            f'<div class="big" style="font-size:clamp(42px,8vw,96px);font-style:italic;">{_esc(d["label"])}</div>'
            f'<div class="sub tight">{_esc(d["description"] or "")}</div>'
            f'<div class="quip">{_esc(quip)}</div>',
            f"{d['start_month']} — {d['end_month']}"
        ))

    # 10. Load-bearing files (MVPs).
    if data["mvp_files"]:
        li_html = "".join(
            f'<li><span class="rank">{i+1}</span><span class="subj">{_esc(f["path"])}</span>'
            f'<span class="meta">{f["surviving_lines"]:,} lines · since {f["first_month"] or "?"}</span></li>'
            for i, f in enumerate(data["mvp_files"][:5])
        )
        cards.append(_card(
            "bg-mvp",
            "the load-bearing files",
            f'<div class="big" style="font-size:clamp(40px,8vw,84px);">MVPs</div>'
            f'<div class="sub">These survived every rewrite. They\'re holding your product up.</div>'
            f'<ul class="list">{li_html}</ul>',
            "delete at your own risk"
        ))

    # 11. What you built (themes).
    if data["top_themes"]:
        max_count = data["top_themes"][0][1] if data["top_themes"] else 1
        tags_html = ""
        for tag, n in data["top_themes"]:
            cls = "huge" if n == max_count else ("big" if n >= max_count * 0.6 else "")
            tags_html += f'<span class="tag {cls}">{_esc(tag)}</span>'
        cards.append(_card(
            "bg-themes",
            "what you built",
            f'<div class="big" style="font-size:clamp(36px,7vw,72px);">you spent this<br>time thinking<br>about…</div>'
            f'<div class="tag-cloud">{tags_html}</div>',
            ""
        ))

    # 12. The arc (from synthesis).
    if data["arc"]:
        cards.append(_card(
            "bg-arc",
            "the arc",
            f'<div class="kicker" style="margin-top:-4px;">in hindsight</div>'
            f'<div class="arc-text">{_esc(data["arc"])}</div>',
            ""
        ))

    # 13. Outro.
    cards.append(_card(
        "bg-outro",
        "",
        f'<div style="flex:1;"></div>'
        f'<div class="outro-title">that\'s<br>a wrap.</div>'
        f'<div class="sub" style="margin-top:28px;">Keep shipping. Keep deleting. See you next year.</div>'
        f'<div class="share-hint"><a href="index.html">← back to the full timeline</a></div>'
        f'<div style="flex:1;"></div>',
        ""
    ))

    # Progress segments.
    progress = "".join("<span></span>" for _ in cards)

    title = f"{repo_name} · wrapped"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{_esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{WRAPPED_CSS}</style>
</head>
<body>
<div class="topbar">
  <a href="index.html">← exit</a>
  <span>{_esc(repo_name)} · wrapped</span>
</div>
<div class="progress">{progress}</div>
<div class="nav-zones">
  <div class="zone prev"></div>
  <div class="zone next"></div>
</div>
<div class="app">
{''.join(cards)}
</div>
<script>{WRAPPED_JS_TEMPLATE}</script>
</body>
</html>"""


def _fmt_month_name(m: str) -> str:
    try:
        d = datetime.strptime(m + "-01", "%Y-%m-%d")
        return d.strftime("%B %Y")
    except (ValueError, TypeError):
        return m or "?"
