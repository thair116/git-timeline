"""Mechanical code-survival analysis.

For each commit we can measure: of the lines it introduced, how many still
exist at HEAD? Aggregated to the month level this gives an objective signal
the LLM hindsight synthesis can use alongside its own judgment.

Algorithm:
  1. List all files at HEAD (git ls-tree -r HEAD), filtered.
  2. For each, run `git blame --line-porcelain` and parse (commit_sha -> line_count).
  3. Aggregate to monthly totals using the commits table for month lookup.
  4. Compare to total insertions per month → survival_ratio.

Stores results in a new table `month_survival`.

Usage:
    python -m src.survival <repo_path> [--db ...]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

from . import db as db_mod
from . import paths

SURVIVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS month_survival (
    month              TEXT PRIMARY KEY,
    surviving_lines    INTEGER NOT NULL,
    total_insertions   INTEGER NOT NULL,
    survival_ratio     REAL NOT NULL,
    files_remaining    INTEGER NOT NULL,
    files_touched      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS file_survival (
    path              TEXT PRIMARY KEY,
    surviving_lines   INTEGER NOT NULL,
    first_author_sha  TEXT,
    first_month       TEXT
);
"""

SKIP_EXTENSIONS = (
    ".lock", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".ico", ".pdf", ".ttf", ".otf", ".woff", ".woff2",
    ".mp3", ".mp4", ".wav", ".mov", ".zip", ".gz", ".tar",
    ".min.js", ".min.css", ".map",
    ".snap",  # jest snapshots
)

SKIP_DIR_SEGMENTS = {
    "node_modules", ".git", "dist", "build", "venv", ".venv",
    "__pycache__", ".next", ".expo", ".expo-shared",
    "ios/Pods", "android/build",
}

SKIP_BASENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "Gemfile.lock", "Cargo.lock", "poetry.lock", "uv.lock",
    "Pipfile.lock", "composer.lock", "go.sum", "mix.lock",
    "Podfile.lock",
}


def should_skip(path: str) -> bool:
    if any(seg in SKIP_DIR_SEGMENTS for seg in path.split("/")):
        return True
    base = path.rsplit("/", 1)[-1]
    if base in SKIP_BASENAMES:
        return True
    if any(path.endswith(ext) for ext in SKIP_EXTENSIONS):
        return True
    return False


def head_files(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [p for p in out.splitlines() if p and not should_skip(p)]


def blame_file(repo: Path, path: str) -> Counter:
    """Return Counter(sha -> lines) for `path` at HEAD, skipping binary files."""
    c: Counter = Counter()
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "blame", "--line-porcelain", "--", path],
            capture_output=True, text=True, check=False,
            errors="replace",
        )
    except Exception:
        return c
    if out.returncode != 0:
        return c
    # Each line starts with `<sha> <orig_lineno> <final_lineno> [<group_size>]\n`
    # followed by header fields, then a content line starting with \t.
    for line in out.stdout.splitlines():
        if not line or line[0] in (" ", "\t"):
            continue
        parts = line.split(" ", 3)
        if len(parts) < 3:
            continue
        sha = parts[0]
        if len(sha) != 40 or not all(ch in "0123456789abcdef" for ch in sha):
            continue
        # Header lines only: distinguish from field lines (author, committer, etc.)
        # by checking that the 2nd/3rd fields are integers.
        if not parts[1].isdigit() or not parts[2].isdigit():
            continue
        c[sha] += 1
    return c


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", type=Path)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--progress-every", type=int, default=50)
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    db_path = args.db or paths.db_path_for(repo.name)
    conn = db_mod.open_db(db_path)
    conn.executescript(SURVIVAL_SCHEMA)

    # sha -> month lookup.
    sha_to_month = {
        r["sha"]: r["month"]
        for r in conn.execute("SELECT sha, month FROM commits")
    }

    head_paths = head_files(repo)
    print(f"blaming {len(head_paths)} files at HEAD")

    month_lines: Counter = Counter()
    files_remaining: dict[str, set] = defaultdict(set)
    file_surv: Counter = Counter()
    file_first: dict[str, tuple[str, str]] = {}  # path -> (earliest_sha, earliest_month)
    sha_dates = {
        r["sha"]: r["committed_at"]
        for r in conn.execute("SELECT sha, committed_at FROM commits")
    }
    for i, path in enumerate(head_paths, 1):
        counts = blame_file(repo, path)
        earliest = None
        for sha, n in counts.items():
            month = sha_to_month.get(sha)
            if month is None:
                continue
            month_lines[month] += n
            files_remaining[month].add(path)
            file_surv[path] += n
            d = sha_dates.get(sha)
            if d and (earliest is None or d < earliest[1]):
                earliest = (sha, d)
        if earliest is not None:
            file_first[path] = (earliest[0], earliest[1][:7])
        if i % args.progress_every == 0 or i == len(head_paths):
            print(f"  [{i:>4}/{len(head_paths)}] tracked {sum(month_lines.values()):,} lines")

    # Files touched per month (for ratio denominator context).
    files_touched: dict[str, set] = defaultdict(set)
    for row in conn.execute("""
        SELECT c.month, f.path FROM commit_files f
        JOIN commits c ON c.sha = f.sha
    """):
        files_touched[row["month"]].add(row["path"])

    # Insertions per month.
    ins_per_month = {
        r["month"]: r["ins"]
        for r in conn.execute(
            "SELECT month, SUM(insertions) AS ins FROM commits GROUP BY month"
        )
    }

    conn.execute("DELETE FROM file_survival")
    for path, n in file_surv.items():
        first_sha, first_month = file_first.get(path, (None, None))
        conn.execute(
            "INSERT INTO file_survival(path, surviving_lines, first_author_sha, first_month) "
            "VALUES (?, ?, ?, ?)",
            (path, n, first_sha, first_month),
        )

    conn.execute("DELETE FROM month_survival")
    print()
    print(f"{'month':8}  {'surv':>8}  {'inserted':>10}  {'ratio':>6}  "
          f"{'files_live':>10}  {'files_touched':>13}")
    for month in sorted(set(list(ins_per_month.keys()) + list(month_lines.keys()))):
        surv = month_lines.get(month, 0)
        ins = ins_per_month.get(month, 0)
        ratio = (surv / ins) if ins > 0 else 0.0
        remain = len(files_remaining.get(month, set()))
        touched = len(files_touched.get(month, set()))
        conn.execute(
            "INSERT INTO month_survival(month, surviving_lines, total_insertions, "
            "survival_ratio, files_remaining, files_touched) VALUES (?, ?, ?, ?, ?, ?)",
            (month, surv, ins, ratio, remain, touched),
        )
        print(f"{month:8}  {surv:>8,}  {ins:>10,}  {ratio:>6.2%}  "
              f"{remain:>10}  {touched:>13}")
    conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
