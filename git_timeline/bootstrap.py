"""M2: Build a compact 'what is this project' anchor for later stages.

Reads a small, fixed budget of files from the target repo at HEAD:
  - README (top-level)
  - package manifests
  - top-level folder listing (1 level deep, no node_modules/venv/etc)
  - root-level .md files (capped)

Outputs:
  - JSON (`bootstrap`): structured project summary
  - long-form text (`bootstrap_text`): stored in `meta` for later prompts

Usage:
    python -m src.bootstrap <repo_path> [--db ...] [--model claude-sonnet-4-6]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import db as db_mod
from . import paths
from .llm import LLMClient, make_client, InSessionClient, PendingAnswer

IGNORE_DIRS = {
    "node_modules", ".git", "dist", "build", "venv", ".venv", "__pycache__",
    ".next", ".expo", "ios/Pods", "android/build", "android/.gradle",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "coverage",
}

MANIFESTS = [
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
    "composer.json",
    "pom.xml",
    "build.gradle",
    "requirements.txt",
    "app.json",          # Expo/RN
    "tsconfig.json",
]

README_CANDIDATES = ["README.md", "README", "README.rst", "Readme.md", "readme.md"]

MAX_FILE_BYTES = 8_000   # per-file cap
MAX_TOTAL_BYTES = 40_000 # total input cap


def read_capped(p: Path, budget: int) -> tuple[str, int]:
    try:
        raw = p.read_bytes()
    except OSError:
        return "", 0
    if len(raw) > min(budget, MAX_FILE_BYTES):
        raw = raw[: min(budget, MAX_FILE_BYTES)]
        truncated = True
    else:
        truncated = False
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return "", 0
    if truncated:
        text += "\n\n[...truncated]"
    return text, len(raw)


def top_level_listing(repo: Path) -> str:
    """git ls-tree-based listing, 2 levels deep, filtered."""
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    seen_dirs: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split("/")
        if any(seg in IGNORE_DIRS for seg in parts):
            continue
        if len(parts) == 1:
            key = parts[0]
        else:
            key = parts[0] + "/"
            if len(parts) >= 3:
                # Track 2nd-level subdir existence.
                sub = parts[0] + "/" + parts[1] + "/"
                seen_dirs[sub] = seen_dirs.get(sub, 0) + 1
        seen_dirs[key] = seen_dirs.get(key, 0) + 1

    lines = []
    for name in sorted(seen_dirs):
        count = seen_dirs[name]
        if name.endswith("/"):
            lines.append(f"{name}  ({count} files)")
        else:
            lines.append(name)
    return "\n".join(lines[:200])  # hard cap


def gather_inputs(repo: Path) -> tuple[str, list[str]]:
    """Return (packed_context, list_of_sources)."""
    sources: list[str] = []
    parts: list[str] = []
    budget = MAX_TOTAL_BYTES

    # README
    for name in README_CANDIDATES:
        p = repo / name
        if p.exists() and p.is_file():
            text, n = read_capped(p, budget)
            if text:
                parts.append(f"=== {name} ===\n{text}\n")
                sources.append(name)
                budget -= n
                break

    # Manifests (first 4 that exist).
    found = 0
    for name in MANIFESTS:
        p = repo / name
        if p.exists() and p.is_file() and found < 4:
            text, n = read_capped(p, budget)
            if text:
                parts.append(f"=== {name} ===\n{text}\n")
                sources.append(name)
                budget -= n
                found += 1

    # Directory listing.
    listing = top_level_listing(repo)
    parts.append(f"=== project file tree ===\n{listing}\n")
    sources.append("<tree>")

    return "\n".join(parts), sources


SYSTEM = """You are a codebase analyst. Given a small bundle of files (README, manifests, file tree) from a project, produce a structured understanding of it.

Output strict JSON only, no prose, with keys:
  purpose:        1-2 sentences on what the project is.
  stack:          list of primary technologies/frameworks (short strings).
  main_features:  list of 3-8 user-facing capabilities inferred from the files.
  architecture:   1-3 sentences on structural layout (services, folders, layers).
  tech_debt:      list of any tech-debt hints visible in the inputs (TODO markers, legacy deps, etc).
  unknowns:       list of things you could not determine from this input.
  anchor:         A 300-500 word paragraph-form description suitable as a system-prompt anchor for downstream analysis. Dense, no bullet points, no fluff."""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", type=Path)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--dry-run", action="store_true",
                    help="Gather inputs but don't call the API")
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    db_path = args.db or paths.db_path_for(repo.name)
    conn = db_mod.open_db(db_path)

    packed, sources = gather_inputs(repo)
    print(f"bootstrap inputs: {len(sources)} sources, {len(packed):,} chars")
    print("  -", "\n  - ".join(sources))

    if args.dry_run:
        out_dir = paths.output_dir()
        (out_dir / f"{repo.name}.bootstrap_input.txt").write_text(packed)
        print(f"dry-run: wrote packed input to {out_dir / (repo.name + '.bootstrap_input.txt')}")
        return 0

    client = make_client(conn)
    if isinstance(client, InSessionClient):
        client.ingest_answers(repo.name, "bootstrap")
    try:
        parsed, result = client.call_json(
            stage="bootstrap",
            key=repo.name,
            model=args.model,
            system=SYSTEM,
            user=packed,
            max_tokens=2048,
        )
    except PendingAnswer:
        return client.finish_pending(repo.name, "bootstrap")

    db_mod.set_meta(conn, "bootstrap_json", json.dumps(parsed, indent=2))
    db_mod.set_meta(conn, "bootstrap_anchor", parsed.get("anchor", ""))
    db_mod.set_meta(conn, "bootstrap_model", args.model)
    conn.commit()

    out_dir = paths.output_dir()
    (out_dir / f"{repo.name}.bootstrap.json").write_text(json.dumps(parsed, indent=2))

    print()
    print(f"model    : {result.model}")
    print(f"cached   : {result.cached}")
    print(f"tokens   : {result.input_tokens} in / {result.output_tokens} out")
    print(f"cost     : ${result.cost:.4f}")
    print()
    print(f"purpose  : {parsed.get('purpose', '')}")
    print(f"stack    : {', '.join(parsed.get('stack', []))}")
    print(f"features : {len(parsed.get('main_features', []))} listed")
    print(f"anchor   : {len(parsed.get('anchor', ''))} chars "
          f"(saved to meta.bootstrap_anchor)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
