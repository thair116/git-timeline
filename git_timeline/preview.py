"""Cost-preview: classify all commits with rules, report LLM burden + $ estimate.

Runs entirely offline. No API key needed.

Usage:
    python -m src.preview [--db cache/<name>.db]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from collections import Counter

from . import db as db_mod
from . import paths
from .filters import classify, needs_diff_expansion

# Rough Haiku 4.5 pricing as of 2026-04 ($/M tokens).
# Adjust here if pricing changes; this is a preview only.
HAIKU_IN = 1.00
HAIKU_OUT = 5.00

# Rough tokens per commit (empirical guesses, refine after first real runs).
TOKENS_LIGHT_IN = 400    # subject + metadata + file list only
TOKENS_LIGHT_OUT = 100
TOKENS_DIFF_IN = 3500    # + truncated diff
TOKENS_DIFF_OUT = 150


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, required=True,
                    help="Path to analyzed SQLite DB (usually ~/.git-timeline/cache/<repo>.db)")
    args = ap.parse_args(argv)

    conn = db_mod.open_db(args.db)

    # Load all commits with their paths in one pass.
    commits = {
        row["sha"]: dict(row)
        for row in conn.execute("SELECT * FROM commits")
    }
    paths_by_sha: dict[str, list[str]] = {sha: [] for sha in commits}
    for row in conn.execute("SELECT sha, path FROM commit_files"):
        paths_by_sha[row["sha"]].append(row["path"])

    rule_counter: Counter[str] = Counter()
    llm_light = 0
    llm_diff = 0

    for sha, commit in commits.items():
        paths = paths_by_sha.get(sha, [])
        result = classify(commit, paths)
        if result is not None:
            rule_counter[result["kind"]] += 1
            continue
        # Needs LLM.
        if needs_diff_expansion(commit) or commit["files_changed"] > 30:
            llm_diff += 1
        else:
            llm_light += 1

    total = len(commits)
    rule_total = sum(rule_counter.values())

    print(f"total commits       : {total}")
    print(f"rule-classified     : {rule_total} ({rule_total / total:.1%})")
    for k, c in rule_counter.most_common():
        print(f"  {k:12}: {c}")
    print()
    print(f"LLM (light)         : {llm_light}")
    print(f"LLM (with diff)     : {llm_diff}")
    llm_total = llm_light + llm_diff
    print(f"LLM total           : {llm_total} ({llm_total / total:.1%})")

    in_tokens = llm_light * TOKENS_LIGHT_IN + llm_diff * TOKENS_DIFF_IN
    out_tokens = llm_light * TOKENS_LIGHT_OUT + llm_diff * TOKENS_DIFF_OUT
    cost = in_tokens / 1_000_000 * HAIKU_IN + out_tokens / 1_000_000 * HAIKU_OUT

    print()
    print(f"est. input tokens   : {in_tokens:>10,}")
    print(f"est. output tokens  : {out_tokens:>10,}")
    print(f"est. Haiku cost     : ${cost:,.2f}")
    print()
    print("(estimate uses rough per-commit averages; actuals recorded after M3)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
