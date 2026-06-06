"""Code-survival curve via cohort analysis (Kaplan-Meier-style).

Our `survival.py` measures one number: of the lines a month inserted, how many
survive to HEAD. That's recency-biased — code written last week scores ~100%
simply because it hasn't had time to die.

This stage fixes that by measuring survival as a function of *age*, with proper
censoring. For a set of monthly snapshots we blame each snapshot's tree and
bucket surviving lines by the month they were originally written (their
"cohort"). A cohort's line count can only fall over time, so:

    S(age) = (lines from cohorts still alive `age` months after writing)
             ----------------------------------------------------------
             (those cohorts' baseline line counts)

Young cohorts only contribute to small ages, so recent code never inflates the
long-age survival — that's the censoring the naive ratio ignores. The age where
S(age) crosses 0.5 is the project's **code half-life**.

This is the git-of-theseus cohort method; the statistical framing is Kaplan-
Meier (lines = subjects, insert = birth, rewrite/delete = death, alive at HEAD =
right-censored). No external deps — the aggregation is exact for this case.

Usage:
    python -m git_timeline.survival_curve <repo_path> [--db ...] [--max-snapshots N] [--workers N]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db as db_mod
from . import paths
from .survival import should_skip

CURVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS code_cohort (
    snapshot_month TEXT NOT NULL,
    cohort_month   TEXT NOT NULL,
    lines          INTEGER NOT NULL,
    PRIMARY KEY (snapshot_month, cohort_month)
);

CREATE TABLE IF NOT EXISTS survival_curve (
    age_months INTEGER PRIMARY KEY,
    survival   REAL NOT NULL,
    n_cohorts  INTEGER NOT NULL,
    base_lines INTEGER NOT NULL
);
"""


def months_between(a: str, b: str) -> int:
    """Whole months from a to b (both 'YYYY-MM'); negative if b precedes a."""
    ay, am = (int(x) for x in a.split("-"))
    by, bm = (int(x) for x in b.split("-"))
    return (by - ay) * 12 + (bm - am)


def tree_files(repo: Path, rev: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", rev],
        capture_output=True, text=True, check=True, errors="replace",
    ).stdout
    return [p for p in out.splitlines() if p and not should_skip(p)]


def blame_at(repo: Path, rev: str, path: str) -> Counter:
    """Counter(sha -> surviving line count) for `path` as of `rev`."""
    c: Counter = Counter()
    out = subprocess.run(
        ["git", "-C", str(repo), "blame", "--line-porcelain", rev, "--", path],
        capture_output=True, text=True, check=False, errors="replace",
    )
    if out.returncode != 0:
        return c
    for line in out.stdout.splitlines():
        if not line or line[0] in (" ", "\t"):
            continue
        parts = line.split(" ", 3)
        if len(parts) < 3:
            continue
        sha = parts[0]
        if len(sha) != 40 or not all(ch in "0123456789abcdef" for ch in sha):
            continue
        if not parts[1].isdigit() or not parts[2].isdigit():
            continue
        c[sha] += 1
    return c


def pick_snapshots(commits: list[dict], max_snapshots: int) -> list[tuple[str, str]]:
    """One snapshot (last commit) per month; evenly sample if there are too many.
    Returns [(month, sha), ...] sorted by month."""
    by_month: dict[str, tuple[str, str]] = {}  # month -> (committed_at, sha)
    for c in commits:
        prev = by_month.get(c["month"])
        if prev is None or c["committed_at"] > prev[0]:
            by_month[c["month"]] = (c["committed_at"], c["sha"])
    months = sorted(by_month)
    snaps = [(m, by_month[m][1]) for m in months]
    if len(snaps) <= max_snapshots:
        return snaps
    # Even sample, always keeping first and last.
    step = (len(snaps) - 1) / (max_snapshots - 1)
    idxs = sorted({round(i * step) for i in range(max_snapshots)})
    return [snaps[i] for i in idxs]


def cohort_matrix(repo: Path, snapshots: list[tuple[str, str]],
                  sha_to_month: dict[str, str], workers: int) -> dict[str, Counter]:
    """{snapshot_month: Counter(cohort_month -> surviving lines)}."""
    matrix: dict[str, Counter] = {}
    for snap_month, sha in snapshots:
        files = tree_files(repo, sha)
        cohorts: Counter = Counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for counts in pool.map(lambda p: blame_at(repo, sha, p), files):
                for s, n in counts.items():
                    m = sha_to_month.get(s)
                    if m is not None:
                        cohorts[m] += n
        matrix[snap_month] = cohorts
        print(f"  snapshot {snap_month}: {len(files):>4} files, "
              f"{sum(cohorts.values()):>8,} lines across {len(cohorts)} cohorts")
    return matrix


def build_curve(matrix: dict[str, Counter]) -> tuple[list[dict], float | None]:
    """Aggregate cohort survival into S(age) and a half-life (in months).

    Baseline for cohort c = its line count at the earliest snapshot whose month
    is >= c (cohort sizes are monotonically non-increasing, so that's its max).
    """
    snap_months = sorted(matrix)
    cohorts = sorted({c for cnt in matrix.values() for c in cnt})

    # baseline[c] = (baseline_snapshot_month, lines)
    baseline: dict[str, tuple[str, int]] = {}
    for c in cohorts:
        for sm in snap_months:
            if months_between(c, sm) >= 0 and matrix[sm].get(c, 0) > 0:
                baseline[c] = (sm, matrix[sm][c])
                break

    # Aggregate by age (months since the cohort's baseline snapshot).
    num: dict[int, int] = defaultdict(int)
    den: dict[int, int] = defaultdict(int)
    seen: dict[int, set] = defaultdict(set)
    for c, (base_sm, base_lines) in baseline.items():
        for sm in snap_months:
            age = months_between(base_sm, sm)
            if age < 0:
                continue
            num[age] += matrix[sm].get(c, 0)
            den[age] += base_lines
            seen[age].add(c)

    curve = [
        {"age": a, "survival": num[a] / den[a], "n_cohorts": len(seen[a]),
         "base_lines": den[a]}
        for a in sorted(num) if den[a] > 0
    ]

    # Half-life: first age where survival drops to 0.5, linearly interpolated.
    half_life = None
    for i in range(1, len(curve)):
        s0, s1 = curve[i - 1]["survival"], curve[i]["survival"]
        if s0 >= 0.5 > s1:
            a0, a1 = curve[i - 1]["age"], curve[i]["age"]
            half_life = a0 + (a1 - a0) * (s0 - 0.5) / (s0 - s1)
            break
    return curve, half_life


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", type=Path)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--max-snapshots", type=int, default=36)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    db_path = args.db or paths.db_path_for(repo.name)
    conn = db_mod.open_db(db_path)
    conn.executescript(CURVE_SCHEMA)

    commits = [dict(r) for r in conn.execute(
        "SELECT sha, month, committed_at FROM commits")]
    if not commits:
        print("No commits. Run extract first.", file=sys.stderr)
        return 2
    sha_to_month = {c["sha"]: c["month"] for c in commits}

    snapshots = pick_snapshots(commits, args.max_snapshots)
    print(f"blaming {len(snapshots)} monthly snapshots "
          f"({snapshots[0][0]} → {snapshots[-1][0]}), {args.workers} workers")

    matrix = cohort_matrix(repo, snapshots, sha_to_month, args.workers)
    curve, half_life = build_curve(matrix)

    conn.execute("DELETE FROM code_cohort")
    for sm, cnt in matrix.items():
        for cm, n in cnt.items():
            conn.execute(
                "INSERT OR REPLACE INTO code_cohort(snapshot_month, cohort_month, lines) "
                "VALUES (?, ?, ?)", (sm, cm, n))
    conn.execute("DELETE FROM survival_curve")
    for pt in curve:
        conn.execute(
            "INSERT OR REPLACE INTO survival_curve(age_months, survival, n_cohorts, base_lines) "
            "VALUES (?, ?, ?, ?)",
            (pt["age"], pt["survival"], pt["n_cohorts"], pt["base_lines"]))
    db_mod.set_meta(conn, "code_survival_curve", json.dumps(curve))
    db_mod.set_meta(conn, "code_half_life_months",
                    "" if half_life is None else f"{half_life:.2f}")
    conn.commit()

    print()
    print(f"{'age (mo)':>8}  {'survival':>9}  {'cohorts':>8}")
    for pt in curve:
        print(f"{pt['age']:>8}  {pt['survival']:>8.1%}  {pt['n_cohorts']:>8}")
    print()
    if half_life is not None:
        print(f"code half-life: {half_life:.1f} months")
    else:
        last = curve[-1] if curve else None
        floor = f"{last['survival']:.0%} at {last['age']} mo" if last else "n/a"
        print(f"code half-life: > {curve[-1]['age'] if curve else 0} months "
              f"(survival never drops below 50%; {floor})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
