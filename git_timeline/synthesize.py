"""M6: The hindsight 1-page timeline.

Inputs (already in SQLite):
  - bootstrap_anchor (what this project IS today)
  - month_summaries (per-month theme, shipped, abandoned, tags)
  - month_survival  (mechanical: surviving_lines / total_insertions)
  - aggregate stats (commit volumes, churn ratios)

Process:
  1. Build a table joining month_summaries + month_survival.
  2. Flag months where mechanical vs narrative disagree (high churn + LLM says
     "shipped", or low churn + LLM says "abandoned").
  3. Pass the joined table + anchor to Opus. Ask for a 1-page hindsight
     markdown timeline: productive months, dead ends, unifying thread toward
     the current state.

Usage:
    python -m src.synthesize <repo_path> [--db ...] [--model claude-opus-4-7]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import db as db_mod
from . import paths
from .llm import LLMClient, spend_report, make_client, InSessionClient, PendingAnswer


SYSTEM = """You are writing a one-page hindsight retrospective of a software project's git history.

You receive:
  - A description of what the project IS TODAY.
  - A month-by-month table: LLM-generated theme + shipped/abandoned lists, alongside mechanical signals (commit count, churn ratio, and code-survival ratio = % of code written that month still present at HEAD).

Your job:
  1. Identify which months were truly productive (shipped work survives and aligns with current product) vs which were dead ends (code deleted/abandoned, or tangents unrelated to today's product).
  2. Tell the linear thread: how did early experimental work shape what shipped? Which pivots were load-bearing?
  3. Flag any disagreements between the narrative summary and mechanical survival (e.g., "ambitious shipping month" but near-zero survival → likely rework or reversion).

Output MARKDOWN, exactly as a single page. Structure:

    # <Project name> — hindsight timeline

    **Today:** <1 sentence on what the project is at HEAD>

    **Arc:** <2-3 sentences on the narrative arc across the whole history>

    ## Month-by-month

    | Month | Commits | Survival | Verdict | One-line theme |
    | ... | ... | ... | ✅/🟡/❌ | ... |

    (Verdict: ✅ productive, 🟡 mixed, ❌ dead end / reverted)

    ## Productive threads
    - <bullet on lasting contributions>
    ...

    ## Dead ends (in hindsight)
    - <bullet on things that were tried and didn't stick, with dates>
    ...

    ## Disagreements between narrative and code
    - <bullet when month's LLM summary contradicts mechanical survival>

Keep the whole page under ~600 words. Ground every claim in the data you received. Where data is missing, say so briefly."""


def build_data_section(conn) -> str:
    rows = list(conn.execute("""
        SELECT m.month, m.theme, m.shipped, m.abandoned, m.commit_count,
               m.churn_ratio,
               s.surviving_lines, s.total_insertions, s.survival_ratio
        FROM month_summaries m
        LEFT JOIN month_survival s ON s.month = m.month
        ORDER BY m.month ASC
    """))
    lines = []
    for r in rows:
        shipped_tags = json.loads(r["shipped"] or "{}")
        shipped = shipped_tags.get("shipped", []) if isinstance(shipped_tags, dict) else []
        abandoned = json.loads(r["abandoned"] or "[]")
        tags = shipped_tags.get("tags", []) if isinstance(shipped_tags, dict) else []
        lines.append(
            f"## {r['month']}\n"
            f"commits: {r['commit_count']}\n"
            f"churn_ratio: {r['churn_ratio']:.2f}  "
            f"survival_ratio: "
            f"{(r['survival_ratio'] if r['survival_ratio'] is not None else 0):.2%} "
            f"({r['surviving_lines'] or 0:,} / {r['total_insertions'] or 0:,})\n"
            f"tags: {', '.join(tags)}\n"
            f"theme: {r['theme']}\n"
            f"shipped: {'; '.join(shipped) if shipped else '(none listed)'}\n"
            f"abandoned: {'; '.join(abandoned) if abandoned else '(none listed)'}\n"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", type=Path)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--model", default="claude-opus-4-8")
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    db_path = args.db or paths.db_path_for(repo.name)
    conn = db_mod.open_db(db_path)

    anchor = db_mod.get_meta(conn, "bootstrap_anchor")
    if not anchor:
        print("Run bootstrap first.", file=sys.stderr)
        return 2

    data = build_data_section(conn)
    if not data.strip():
        print("No month summaries found. Run src.months first.", file=sys.stderr)
        return 2

    client = make_client(conn)
    if isinstance(client, InSessionClient):
        client.ingest_answers(repo.name, "synthesize")
    user = f"PROJECT TODAY:\n{anchor}\n\nMONTH DATA:\n{data}"

    try:
        result = client.call(
            stage="synthesize",
            key=repo.name,
            model=args.model,
            system=SYSTEM,
            user=user,
            max_tokens=3000,
            temperature=None,  # Opus rejects the temperature parameter
        )
    except PendingAnswer:
        return client.finish_pending(repo.name, "synthesize")

    out_dir = paths.output_dir()
    out_path = out_dir / f"{repo.name}.timeline.md"
    out_path.write_text(result.text)

    print(f"model  : {result.model}")
    print(f"cached : {result.cached}")
    print(f"tokens : {result.input_tokens} in / {result.output_tokens} out")
    print(f"cost   : ${result.cost:.4f}")
    print(f"output : {out_path}")
    print()
    print("=== spend report ===")
    rep = spend_report(conn)
    for s in rep["stages"]:
        print(f"  {s['stage']:12} {s['model']:30} n={s['n']:4}  "
              f"in={s['input_tokens']:>8,}  out={s['output_tokens']:>6,}  ${s['cost']:.4f}")
    print(f"  {'TOTAL':44} ${rep['total_cost']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
