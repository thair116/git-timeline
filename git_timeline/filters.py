"""Rule-based pre-classification of commits.

Goal: label obvious commits (merges, lockfile bumps, dependabot, trivial docs)
without sending anything to an LLM. For each commit we produce either:
  - a dict with kind/one_liner/signals (skip LLM), or
  - None (needs LLM).

These rules should err on the side of NOT filtering — when in doubt, let the
LLM handle it. The goal is to remove noise, not replace judgment.
"""
from __future__ import annotations

import re
from typing import Iterable

LOCKFILE_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "bun.lock",
    "Gemfile.lock",
    "Cargo.lock",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "composer.lock",
    "go.sum",
    "mix.lock",
    "Podfile.lock",
}

GENERATED_SUFFIXES = (
    ".lock",
    ".min.js",
    ".min.css",
    ".map",
)

DEPENDABOT_AUTHORS = {
    "dependabot[bot]",
    "renovate[bot]",
    "dependabot-preview[bot]",
}

# Subject regexes that strongly imply a specific kind.
SUBJECT_RULES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"^(chore: )?bump .+ (to|from) v?\d", re.I), "chore", "dependency bump"),
    (re.compile(r"^bump version( to)? v?\d", re.I), "chore", "version bump"),
    (re.compile(r"^merge (branch|pull request|remote)", re.I), "merge", "merge"),
    (re.compile(r"^revert ", re.I), "chore", "revert"),
    (re.compile(r"^(typo|fix typo)\b", re.I), "docs", "typo fix"),
    (re.compile(r"^(docs?|readme)[:\s]", re.I), "docs", "docs update"),
]


def _is_all_lockfiles(paths: Iterable[str]) -> bool:
    paths = list(paths)
    if not paths:
        return False
    for p in paths:
        base = p.rsplit("/", 1)[-1]
        if base in LOCKFILE_NAMES:
            continue
        if any(p.endswith(s) for s in GENERATED_SUFFIXES):
            continue
        return False
    return True


def classify(commit: dict, paths: list[str]) -> dict | None:
    """Return a summary dict if the commit can be rule-classified, else None.

    commit: row from `commits` table (dict-like).
    paths:  list of paths from commit_files.
    """
    subject = (commit["subject"] or "").strip()
    author = commit["author_name"]

    # Merge commits carry no standalone diff worth summarizing.
    if commit["is_merge"]:
        return {
            "kind": "merge",
            "one_liner": subject or "merge commit",
            "signals": ["merge"],
            "method": "rule",
        }

    # Dependabot / renovate.
    if author in DEPENDABOT_AUTHORS:
        return {
            "kind": "chore",
            "one_liner": subject[:120] or "dependency update (bot)",
            "signals": ["bot", "dependency"],
            "method": "rule",
        }

    # Lockfile-only touches.
    if _is_all_lockfiles(paths):
        return {
            "kind": "chore",
            "one_liner": f"lockfile update ({len(paths)} file{'s' if len(paths) != 1 else ''})",
            "signals": ["lockfile"],
            "method": "rule",
        }

    # Subject-regex rules.
    for pat, kind, tag in SUBJECT_RULES:
        if pat.search(subject):
            return {
                "kind": kind,
                "one_liner": subject[:120],
                "signals": [tag],
                "method": "rule",
            }

    # Empty or near-empty commits.
    if commit["files_changed"] == 0:
        return {
            "kind": "chore",
            "one_liner": subject[:120] or "(empty commit)",
            "signals": ["empty"],
            "method": "rule",
        }

    return None


def needs_diff_expansion(commit: dict) -> bool:
    """Heuristic: does this commit's subject alone likely not explain it?"""
    subject = (commit["subject"] or "").strip().lower()
    if len(subject) < 12:
        return True
    opaque_words = {
        "wip", "fix", "update", "updates", "fixes", "yolo", "stuff",
        "misc", "tweaks", "cleanup", "refactor", "wtf", "omg",
    }
    # Single-word or all-opaque subjects need the diff.
    words = re.findall(r"[a-z]+", subject)
    if not words:
        return True
    if all(w in opaque_words for w in words):
        return True
    return False
