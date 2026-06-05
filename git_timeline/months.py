"""M5: Roll up per-commit summaries into monthly themes.

For each YYYY-MM with >= 1 commit:
  - gather all (date, kind, subject, one_liner, signals, files_changed, churn)
  - compute mechanical churn_ratio = deletions / (insertions + deletions)
  - call Sonnet with the list; get back {theme, shipped, abandoned, tags}
  - persist to month_summaries

Mechanical signals (churn ratio, commit volume, kind mix) stay in SQLite so
M6 can use them alongside LLM judgment without re-paying.

Usage:
    python -m src.months <repo_path> [--db ...] [--model claude-sonnet-4-6]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import db as db_mod
from . import paths
from .llm import LLMClient, spend_report, make_client, InSessionClient, PendingAnswer

SYSTEM_TEMPLATE = """You analyze a month of git history for a project, synthesizing per-commit summaries into a monthly theme.

PROJECT CONTEXT:
{anchor}

You will receive a list of commits from ONE month, in chronological order. Output STRICT JSON ONLY with keys:
  theme:      1-2 sentences naming the dominant themes of the month.
  shipped:    list of short phrases (3-8 words each) describing things that appear to have actually landed / reached a working state this month.
  abandoned:  list of short phrases describing things that appear to have been abandoned, reverted, or left half-done based on the commit trail.
  tags:       3-6 short tags summarizing the month's focus (e.g. ["auth", "onboarding", "refactor"]).

Base claims on evidence in the commit list. If "shipped" or "abandoned" is not visible, return an empty list — do NOT invent. No markdown, no prose outside JSON."""


USER_TEMPLATE = """Month: {month}
Commits: {n}
Total churn: +{ins} / -{dels}  (churn_ratio: {churn_ratio:.2f})

Kind mix: {kind_mix}

Commits (chronological):
{commit_list}"""


def fmt_commit_line(row: dict) -> str:
    sig = json.loads(row["signals"] or "[]")
    sig_s = " ".join(f"#{s}" for s in sig) if sig else ""
    return (
        f"  {row['committed_at'][:10]} [{row['kind']:8}] "
        f"+{row['insertions']}/-{row['deletions']}  {row['one_liner']} {sig_s}"
    ).rstrip()


def rollup_month(
    conn, month: str, client: LLMClient, system_prompt: str, model: str
) -> tuple[dict, float]:
    rows = list(conn.execute("""
        SELECT c.committed_at, c.insertions, c.deletions, c.subject,
               s.kind, s.one_liner, s.signals
        FROM commits c
        JOIN commit_summaries s ON s.sha = c.sha
        WHERE c.month = ?
        ORDER BY c.committed_at ASC
    """, (month,)))

    if not rows:
        return {}, 0.0

    total_ins = sum(r["insertions"] for r in rows)
    total_dels = sum(r["deletions"] for r in rows)
    denom = total_ins + total_dels
    churn_ratio = total_dels / denom if denom else 0.0

    kind_counts: dict[str, int] = {}
    for r in rows:
        kind_counts[r["kind"]] = kind_counts.get(r["kind"], 0) + 1
    kind_mix = ", ".join(f"{k}={v}" for k, v in sorted(kind_counts.items(), key=lambda kv: -kv[1]))

    commit_list = "\n".join(fmt_commit_line(dict(r)) for r in rows)

    user = USER_TEMPLATE.format(
        month=month,
        n=len(rows),
        ins=total_ins,
        dels=total_dels,
        churn_ratio=churn_ratio,
        kind_mix=kind_mix,
        commit_list=commit_list,
    )

    parsed, result = client.call_json(
        stage="month",
        key=month,
        model=model,
        system=system_prompt,
        user=user,
        max_tokens=800,
    )

    return {
        "month": month,
        "theme": parsed.get("theme", ""),
        "shipped": parsed.get("shipped", []),
        "abandoned": parsed.get("abandoned", []),
        "tags": parsed.get("tags", []),
        "commit_count": len(rows),
        "churn_ratio": churn_ratio,
        "model": result.model,
    }, result.cost


def upsert_month(conn, m: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO month_summaries
           (month, theme, shipped, abandoned, commit_count, churn_ratio, method)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            m["month"],
            m["theme"],
            json.dumps({"shipped": m["shipped"], "tags": m["tags"]}),
            json.dumps(m["abandoned"]),
            m["commit_count"],
            m["churn_ratio"],
            f"llm:{m['model']}",
        ),
    )
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", type=Path)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--model", default="claude-sonnet-4-6")
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    db_path = args.db or paths.db_path_for(repo.name)
    conn = db_mod.open_db(db_path)

    anchor = db_mod.get_meta(conn, "bootstrap_anchor")
    if not anchor:
        print("Run bootstrap first.", file=sys.stderr)
        return 2
    system_prompt = SYSTEM_TEMPLATE.format(anchor=anchor.strip())

    # Ensure all commits have summaries before rolling up.
    missing = conn.execute("""
        SELECT COUNT(*) c FROM commits c
        LEFT JOIN commit_summaries s ON s.sha = c.sha
        WHERE s.sha IS NULL
    """).fetchone()["c"]
    if missing:
        print(f"WARNING: {missing} commits lack summaries; run src.commits first.",
              file=sys.stderr)

    months = [
        r["month"]
        for r in conn.execute(
            "SELECT DISTINCT month FROM commits ORDER BY month ASC"
        )
    ]
    print(f"rolling up {len(months)} months")

    client = make_client(conn)
    if isinstance(client, InSessionClient):
        ing = client.ingest_answers(repo.name, "month")
        if ing:
            print(f"in-session : ingested {ing} answer(s) from previous round")

    total_cost = 0.0
    for month in months:
        try:
            m, cost = rollup_month(conn, month, client, system_prompt, args.model)
        except PendingAnswer:
            continue
        if not m:
            continue
        upsert_month(conn, m)
        total_cost += cost
        tags = ",".join(m["tags"][:4])
        print(f"  {month}  n={m['commit_count']:>3}  churn={m['churn_ratio']:.2f}  "
              f"[{tags}]  ${cost:.4f}")

    if isinstance(client, InSessionClient) and client.pending:
        return client.finish_pending(repo.name, "month")

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
