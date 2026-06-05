"""M1: Extract git history into SQLite.

Usage:
    python -m src.extract <repo_path> [--ref main] [--db cache/<name>.db]

Parses `git log --numstat` in a single pass using a sentinel-prefixed format.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import db as db_mod
from . import paths

# Field sep = \x1f (unit separator); record sep implied by newline.
# Fields: sha, committed_at (ISO), author_name, author_email, parents, subject, body
SENTINEL = "§COMMIT§"
FS = "\x1f"
# Use %x00 in body position? No — null bytes break text parsing. We use
# %B (full message) but delimit with a second sentinel to keep line-based parse.
BODY_END = "§ENDBODY§"

GIT_FORMAT = f"{SENTINEL}{FS}%H{FS}%aI{FS}%an{FS}%ae{FS}%P{FS}%s{FS}%B{BODY_END}"


def run_git_log(repo: Path, ref: str) -> str:
    cmd = [
        "git",
        "-C",
        str(repo),
        "log",
        "--reverse",
        "--numstat",
        f"--pretty=format:{GIT_FORMAT}",
        ref,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return out.stdout


def count_commits(repo: Path, ref: str) -> int:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", ref],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def parse_log(raw: str):
    """Yield (commit_dict, [(path, ins, dels), ...]) tuples."""
    # Split on the sentinel. First chunk is empty (leading sentinel).
    chunks = raw.split(SENTINEL)
    for chunk in chunks:
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        # chunk starts with FS; split once to drop the empty prefix.
        if chunk.startswith(FS):
            chunk = chunk[1:]

        # Header line: fields separated by FS, ending with body terminated by BODY_END.
        head, _, after_body = chunk.partition(BODY_END)
        parts = head.split(FS)
        if len(parts) < 7:
            # malformed; skip
            continue
        sha, committed_at, author_name, author_email, parents, subject, body = parts[:7]

        numstat_lines = [ln for ln in after_body.splitlines() if ln.strip()]
        files: list[tuple[str, int, int]] = []
        for ln in numstat_lines:
            cols = ln.split("\t")
            if len(cols) != 3:
                continue
            ins_s, del_s, path = cols
            # Binary files show '-' for counts.
            ins = int(ins_s) if ins_s.isdigit() else 0
            dels = int(del_s) if del_s.isdigit() else 0
            files.append((path, ins, dels))

        parent_list = parents.split() if parents else []
        is_merge = 1 if len(parent_list) > 1 else 0
        total_ins = sum(f[1] for f in files)
        total_dels = sum(f[2] for f in files)

        yield (
            {
                "sha": sha,
                "committed_at": committed_at,
                "author_name": author_name,
                "author_email": author_email,
                "parents": parents,
                "is_merge": is_merge,
                "subject": subject,
                "body": body.strip() or None,
                "files_changed": len(files),
                "insertions": total_ins,
                "deletions": total_dels,
            },
            files,
        )


def insert_commits(conn, commits_iter) -> int:
    count = 0
    cur = conn.cursor()
    for commit, files in commits_iter:
        cur.execute(
            """
            INSERT OR REPLACE INTO commits
            (sha, committed_at, author_name, author_email, parents, is_merge,
             subject, body, files_changed, insertions, deletions)
            VALUES (:sha, :committed_at, :author_name, :author_email, :parents,
                    :is_merge, :subject, :body, :files_changed, :insertions,
                    :deletions)
            """,
            commit,
        )
        cur.execute("DELETE FROM commit_files WHERE sha = ?", (commit["sha"],))
        if files:
            cur.executemany(
                "INSERT INTO commit_files(sha, path, insertions, deletions) "
                "VALUES (?, ?, ?, ?)",
                [(commit["sha"], p, i, d) for (p, i, d) in files],
            )
        count += 1
    conn.commit()
    return count


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Extract git history into SQLite.")
    ap.add_argument("repo", type=Path, help="Path to git repository")
    ap.add_argument("--ref", default="HEAD", help="Ref to walk (default: HEAD)")
    ap.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite path (default: cache/<repo_name>.db)",
    )
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    if not (repo / ".git").exists():
        print(f"not a git repo: {repo}", file=sys.stderr)
        return 2

    db_path = args.db or paths.db_path_for(repo.name)

    expected = count_commits(repo, args.ref)
    print(f"repo     : {repo}")
    print(f"ref      : {args.ref}")
    print(f"expected : {expected} commits")
    print(f"db       : {db_path}")

    print("running git log...")
    raw = run_git_log(repo, args.ref)
    print(f"log size : {len(raw):,} bytes")

    conn = db_mod.open_db(db_path)
    db_mod.set_meta(conn, "repo_path", str(repo))
    db_mod.set_meta(conn, "ref", args.ref)

    inserted = insert_commits(conn, parse_log(raw))
    actual = conn.execute("SELECT COUNT(*) AS c FROM commits").fetchone()["c"]

    print(f"inserted : {inserted}")
    print(f"in db    : {actual}")
    print(f"match    : {'OK' if actual == expected else 'MISMATCH'}")

    # Quick sanity stats.
    row = conn.execute(
        "SELECT MIN(committed_at) AS first, MAX(committed_at) AS last, "
        "SUM(is_merge) AS merges FROM commits"
    ).fetchone()
    months = conn.execute(
        "SELECT COUNT(DISTINCT month) AS n FROM commits"
    ).fetchone()["n"]
    print(f"range    : {row['first']} -> {row['last']}")
    print(f"merges   : {row['merges']}")
    print(f"months   : {months}")

    return 0 if actual == expected else 1


if __name__ == "__main__":
    sys.exit(main())
