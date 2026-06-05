"""M7a: Generate a tech-tree DAG from analyzed git history.

Asks an LLM to decompose the project's history into ~30–45 technical threads
(subsystems/features/experiments), with evolution edges between them. Persists
to tree_nodes / tree_edges tables for render.py to draw as a swimlane DAG.

Usage:
    python -m src.tree <repo_path> [--db ...] [--model claude-sonnet-4-6]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import db as db_mod
from . import paths
from .llm import LLMClient, spend_report, make_client, InSessionClient, PendingAnswer


SYSTEM = """You are building a tech-tree DAG of a software project's evolution from its git history.

INPUT: project context, month-by-month summaries with shipped/abandoned items, survival ratios, and a narrative arc.

OUTPUT: STRICT JSON ONLY. No markdown fences, no prose. Schema:

{
  "nodes": [
    {
      "id": "kebab-case-id",
      "label": "Human name (max 36 chars)",
      "category": "infra" | "backend" | "data" | "audio" | "ui" | "auth" | "monetization" | "content" | "gamification" | "ops" | "experiment",
      "start_month": "YYYY-MM",
      "end_month": "YYYY-MM" (or null if the thread is still live at HEAD),
      "status": "live" | "superseded" | "dead",
      "description": "One factual sentence grounded in the input data."
    }
  ],
  "edges": [
    { "from": "node-id", "to": "node-id", "kind": "replaced-by" | "evolved-into" | "enabled" | "branched-from" }
  ]
}

GUIDELINES:
- Produce 25-45 nodes. Each node represents a distinct technical thread — a subsystem, framework choice, feature domain, or experiment — NOT an individual commit and NOT a whole month.
- Status rules (enforce strictly):
    * "live"       = this thread's code/system still exists at HEAD and is active.
    * "superseded" = replaced by a successor that IS present; MUST have an outgoing replaced-by OR evolved-into edge.
    * "dead"       = abandoned with no direct successor; do NOT connect it with replaced-by.
- Edge rules:
    * "evolved-into": same conceptual thread, matured (e.g., scaffold → v2 of scaffold).
    * "replaced-by": one technology/approach supplanted by another (e.g., Celery → Prefect).
    * "enabled":    prerequisite → dependent; use sparingly, only for NON-OBVIOUS dependencies.
    * "branched-from": a new thread that forked from an existing one.
- end_month of a superseded node should equal (or be close to) start_month of its successor.
- Cover BOTH productive threads and dead ends visible in the data. Roughly 1/3 of nodes should be dead or superseded.
- Every id MUST be unique, valid kebab-case, and every edge endpoint must reference a defined node.
- Keep labels short and specific (e.g., "react-native-track-player", not "audio library").
- Ground every node in something named in the monthly data or narrative. Do NOT invent.
"""


def build_input(conn) -> str:
    anchor = db_mod.get_meta(conn, "bootstrap_anchor") or ""

    rows = list(conn.execute("""
        SELECT m.month, m.theme, m.shipped, m.abandoned, m.commit_count,
               m.churn_ratio,
               sv.survival_ratio
        FROM month_summaries m
        LEFT JOIN month_survival sv ON sv.month = m.month
        ORDER BY m.month ASC
    """))
    month_lines = []
    for r in rows:
        shipped_blob = json.loads(r["shipped"] or "{}")
        shipped = shipped_blob.get("shipped", []) if isinstance(shipped_blob, dict) else []
        tags = shipped_blob.get("tags", []) if isinstance(shipped_blob, dict) else []
        abandoned = json.loads(r["abandoned"] or "[]")
        surv = r["survival_ratio"]
        surv_s = f"{surv:.0%}" if surv is not None else "n/a"
        month_lines.append(
            f"## {r['month']}\n"
            f"commits={r['commit_count']} churn={r['churn_ratio']:.2f} survival={surv_s}\n"
            f"tags: {', '.join(tags)}\n"
            f"theme: {r['theme']}\n"
            f"shipped: {'; '.join(shipped) if shipped else '(none)'}\n"
            f"abandoned: {'; '.join(abandoned) if abandoned else '(none)'}\n"
        )

    synth_row = conn.execute(
        "SELECT response FROM llm_cache WHERE stage = 'synthesize' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    synthesis = synth_row["response"] if synth_row else ""

    return (
        f"PROJECT TODAY:\n{anchor}\n\n"
        f"MONTH DATA:\n" + "\n".join(month_lines) + "\n\n"
        f"NARRATIVE (previously synthesized, for context only):\n{synthesis}"
    )


VALID_CATEGORIES = {
    "infra", "backend", "data", "audio", "ui", "auth",
    "monetization", "content", "gamification", "ops", "experiment",
    "ios-native",
}
VALID_STATUSES = {"live", "superseded", "dead"}
VALID_KINDS = {"evolved-into", "replaced-by", "enabled", "branched-from"}


def validate(dag: dict) -> list[str]:
    errors: list[str] = []
    nodes = dag.get("nodes", [])
    edges = dag.get("edges", [])
    ids = {n.get("id") for n in nodes}
    if len(ids) != len(nodes):
        errors.append("duplicate node ids")
    for n in nodes:
        nid = n.get("id", "?")
        if n.get("category") not in VALID_CATEGORIES:
            errors.append(f"{nid}: bad category {n.get('category')!r}")
        if n.get("status") not in VALID_STATUSES:
            errors.append(f"{nid}: bad status {n.get('status')!r}")
        if not n.get("start_month"):
            errors.append(f"{nid}: missing start_month")
    for e in edges:
        if e.get("from") not in ids:
            errors.append(f"edge from unknown {e.get('from')!r}")
        if e.get("to") not in ids:
            errors.append(f"edge to unknown {e.get('to')!r}")
        if e.get("kind") not in VALID_KINDS:
            errors.append(f"edge bad kind {e.get('kind')!r}")
    # Superseded must have an outgoing replaced-by or evolved-into.
    outgoing: dict[str, list[str]] = {}
    for e in edges:
        outgoing.setdefault(e.get("from"), []).append(e.get("kind"))
    for n in nodes:
        if n.get("status") == "superseded":
            kinds = outgoing.get(n.get("id"), [])
            if not any(k in ("replaced-by", "evolved-into") for k in kinds):
                errors.append(f"{n.get('id')}: superseded but has no successor edge")
    return errors


def persist(conn, dag: dict) -> None:
    conn.execute("DELETE FROM tree_edges")
    conn.execute("DELETE FROM tree_nodes")
    for n in dag.get("nodes", []):
        conn.execute(
            "INSERT INTO tree_nodes(id, label, category, start_month, "
            "end_month, status, description) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (n["id"], n["label"], n.get("category"), n.get("start_month"),
             n.get("end_month"), n["status"], n.get("description", "")),
        )
    for e in dag.get("edges", []):
        conn.execute(
            "INSERT OR IGNORE INTO tree_edges(src, dst, kind) VALUES (?, ?, ?)",
            (e["from"], e["to"], e["kind"]),
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

    user = build_input(conn)

    client = make_client(conn)
    if isinstance(client, InSessionClient):
        client.ingest_answers(repo.name, "tree")
    try:
        parsed, result = client.call_json(
            stage="tree",
            key=repo.name,
            model=args.model,
            system=SYSTEM,
            user=user,
            max_tokens=6000,
        )
    except PendingAnswer:
        return client.finish_pending(repo.name, "tree")

    errors = validate(parsed)
    if errors:
        print("Validation errors:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        # Still persist, but warn.

    persist(conn, parsed)

    n_nodes = len(parsed.get("nodes", []))
    n_edges = len(parsed.get("edges", []))
    by_status = {"live": 0, "superseded": 0, "dead": 0}
    by_cat: dict[str, int] = {}
    for n in parsed.get("nodes", []):
        by_status[n.get("status", "unknown")] = by_status.get(n.get("status"), 0) + 1
        c = n.get("category", "?")
        by_cat[c] = by_cat.get(c, 0) + 1

    print(f"model  : {result.model}")
    print(f"tokens : {result.input_tokens} in / {result.output_tokens} out")
    print(f"cost   : ${result.cost:.4f}   cached={result.cached}")
    print(f"nodes  : {n_nodes}  ({', '.join(f'{k}={v}' for k, v in by_status.items())})")
    print(f"edges  : {n_edges}")
    print(f"cats   : {', '.join(f'{k}={v}' for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1]))}")

    out_dir = paths.output_dir()
    (out_dir / f"{repo.name}.tree.json").write_text(json.dumps(parsed, indent=2))

    print()
    print("=== spend report ===")
    rep = spend_report(conn)
    for s in rep["stages"]:
        print(f"  {s['stage']:12} {s['model']:30} n={s['n']:4}  "
              f"in={s['input_tokens']:>8,}  out={s['output_tokens']:>6,}  ${s['cost']:.4f}")
    print(f"  {'TOTAL':44} ${rep['total_cost']:.4f}")

    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
