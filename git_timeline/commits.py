"""M3/M4: Per-commit summarization.

For each commit (earliest first, resumable):
  1. Skip if already summarized.
  2. Try rule-based classification (filters.classify). If hit, write and continue.
  3. Build a compact prompt: metadata + file-stat list (+ truncated diff if needed).
  4. Call Haiku. Parse JSON. Persist.

Usage:
    python -m src.commits <repo_path> [--limit 100] [--db ...] \
                                      [--model claude-haiku-4-5] \
                                      [--max-diff-bytes 15000]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from . import db as db_mod
from . import paths
from .filters import classify, needs_diff_expansion, LOCKFILE_NAMES, GENERATED_SUFFIXES
from .llm import LLMClient, spend_report, make_client, InSessionClient, PendingAnswer

SYSTEM_TEMPLATE = """You analyze individual git commits for a project. Your output is a compact, factual one-liner that will later be summarized into monthly themes.

PROJECT CONTEXT:
{anchor}

For each commit you receive, output STRICT JSON ONLY with keys:
  kind:      one of [feature, fix, refactor, infra, docs, chore, test, style, unclear]
  one_liner: one sentence (<= 140 chars), present tense, starts with a verb, names the thing changed
  signals:   list of 0-4 short tags like ["auth", "onboarding", "experiment", "reverted-later", "large-refactor"]

Do NOT include the commit SHA or author. Do NOT wrap in markdown fences. Do NOT add any prose."""


USER_TEMPLATE = """SHA:      {sha}
Date:     {date}
Author:   {author}
Subject:  {subject}
Body:     {body}

Stats: {files_changed} files, +{insertions}/-{deletions}

Files changed:
{file_list}
{diff_section}"""


def build_file_list(rows: list[dict], cap: int = 30) -> str:
    if not rows:
        return "(no files)"
    lines = []
    for r in rows[:cap]:
        lines.append(f"  +{r['insertions']:>5} -{r['deletions']:>5}  {r['path']}")
    if len(rows) > cap:
        lines.append(f"  ... and {len(rows) - cap} more files")
    return "\n".join(lines)


def get_diff(repo: Path, sha: str, max_bytes: int) -> str:
    """Fetch the commit diff, excluding lockfiles, capped in size."""
    pathspec_excludes = []
    for name in LOCKFILE_NAMES:
        pathspec_excludes.append(f":(exclude){name}")
    for suf in GENERATED_SUFFIXES:
        pathspec_excludes.append(f":(exclude)*{suf}")

    cmd = [
        "git", "-C", str(repo), "show", sha,
        "--pretty=",
        "--unified=1",
        "--no-color",
        "--",
    ] + pathspec_excludes

    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True,
                             errors="replace")
    except subprocess.CalledProcessError as e:
        return f"[diff unavailable: {e}]"

    diff = out.stdout
    if len(diff) > max_bytes:
        diff = diff[:max_bytes] + f"\n\n[...truncated at {max_bytes} bytes of diff]"
    return diff


def summarize_commit(
    repo: Path,
    commit: dict,
    file_rows: list[dict],
    client: LLMClient,
    system_prompt: str,
    model: str,
    max_diff_bytes: int,
) -> dict:
    """Return a summary dict ready to insert into commit_summaries."""
    # Rule filter first.
    rule = classify(commit, [r["path"] for r in file_rows])
    if rule is not None:
        return rule

    # Decide whether to include diff.
    include_diff = needs_diff_expansion(commit) or commit["files_changed"] > 30
    diff_section = ""
    if include_diff:
        diff = get_diff(repo, commit["sha"], max_diff_bytes)
        diff_section = f"\nDiff (excludes lockfiles/generated, may be truncated):\n```\n{diff}\n```"

    user = USER_TEMPLATE.format(
        sha=commit["sha"][:12],
        date=commit["committed_at"],
        author=commit["author_name"],
        subject=commit["subject"],
        body=(commit["body"] or "")[:500],
        files_changed=commit["files_changed"],
        insertions=commit["insertions"],
        deletions=commit["deletions"],
        file_list=build_file_list(file_rows),
        diff_section=diff_section,
    )

    parsed, result = client.call_json(
        stage="commit",
        key=commit["sha"],
        model=model,
        system=system_prompt,
        user=user,
        max_tokens=300,
    )

    return {
        "kind": parsed.get("kind", "unclear"),
        "one_liner": parsed.get("one_liner", commit["subject"])[:200],
        "signals": parsed.get("signals", []),
        "method": f"llm:{result.model}",
        "_result": result,
    }


def upsert_summary(conn, sha: str, summary: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO commit_summaries(sha, kind, one_liner, signals, method) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            sha,
            summary["kind"],
            summary["one_liner"],
            json.dumps(summary.get("signals", [])),
            summary["method"],
        ),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", type=Path)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--limit", type=int, default=None,
                    help="Only summarize the first N unresolved commits")
    ap.add_argument("--model", default="claude-haiku-4-5")
    ap.add_argument("--max-diff-bytes", type=int, default=15_000)
    ap.add_argument("--budget", type=float, default=None,
                    help="Stop if running cost exceeds this $ amount")
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    db_path = args.db or paths.db_path_for(repo.name)
    conn = db_mod.open_db(db_path)

    anchor = db_mod.get_meta(conn, "bootstrap_anchor")
    if not anchor:
        print("No bootstrap anchor found. Run `python -m src.bootstrap <repo>` first.",
              file=sys.stderr)
        return 2
    system_prompt = SYSTEM_TEMPLATE.format(anchor=anchor.strip())

    client = make_client(conn)
    if isinstance(client, InSessionClient):
        ing = client.ingest_answers(repo.name, "commit")
        if ing:
            print(f"in-session : ingested {ing} answer(s) from previous round")

    # Commits in chronological order, skipping any already summarized.
    rows = conn.execute(
        """
        SELECT c.* FROM commits c
        LEFT JOIN commit_summaries s ON s.sha = c.sha
        WHERE s.sha IS NULL
        ORDER BY c.committed_at ASC
        """
    ).fetchall()

    if args.limit is not None:
        rows = rows[: args.limit]

    total = len(rows)
    total_all = conn.execute("SELECT COUNT(*) c FROM commits").fetchone()["c"]
    done_already = conn.execute(
        "SELECT COUNT(*) c FROM commit_summaries"
    ).fetchone()["c"]
    print(f"to summarize : {total} (already done: {done_already}/{total_all})")
    print(f"model        : {args.model}")
    print()

    running_cost = 0.0
    running_in = 0
    running_out = 0
    rule_hits = 0
    llm_hits = 0
    t0 = time.time()

    for i, row in enumerate(rows, 1):
        commit = dict(row)
        file_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT path, insertions, deletions FROM commit_files WHERE sha = ?",
                (commit["sha"],),
            )
        ]
        try:
            summary = summarize_commit(
                repo, commit, file_rows, client, system_prompt,
                args.model, args.max_diff_bytes,
            )
        except PendingAnswer:
            # In-session: prompt queued, will be answered out of band. Collect
            # the rest of this run's prompts, then emit them after the loop.
            continue
        except Exception as e:
            print(f"  ! {commit['sha'][:8]} failed: {e}")
            continue

        upsert_summary(conn, commit["sha"], summary)

        if summary["method"] == "rule":
            rule_hits += 1
        else:
            llm_hits += 1
            res = summary.get("_result")
            if res is not None and not res.cached:
                running_cost += res.cost
                running_in += res.input_tokens
                running_out += res.output_tokens

        conn.commit()

        if i % 10 == 0 or i == total:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0
            print(
                f"  [{i:>4}/{total}] rule={rule_hits} llm={llm_hits} "
                f"cost=${running_cost:.3f} in={running_in:,} out={running_out:,} "
                f"{rate:.1f} c/s eta={eta:.0f}s"
            )

        if args.budget is not None and running_cost > args.budget:
            print(f"!! budget ${args.budget} exceeded at commit {i}; stopping")
            break

    # In-session: emit the prompts gathered this pass and stop. The agent
    # answers them, then re-runs this stage to persist (now all cache hits).
    if isinstance(client, InSessionClient) and client.pending:
        return client.finish_pending(repo.name, "commit")

    print()
    print("=== spend report ===")
    rep = spend_report(conn)
    for stage in rep["stages"]:
        print(f"  {stage['stage']:12} {stage['model']:30} "
              f"n={stage['n']:4}  in={stage['input_tokens']:>8,}  "
              f"out={stage['output_tokens']:>6,}  ${stage['cost']:.4f}")
    print(f"  {'TOTAL':44} ${rep['total_cost']:.4f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
