---
name: git-timeline
description: Analyze any git repository's full history with LLM summaries and generate an interactive HTML report — hindsight timeline with code-survival analysis, a tech-tree DAG of evolving subsystems and dead ends, and a Spotify-Wrapped-style retrospective. Use when the user asks to visualize git history, analyze a repo's arc, or generate a project retrospective.
allowed-tools: Bash(python3 *), Bash(git *), Bash(open *), Bash(ls *), Bash(mkdir *), Bash(cp *), Bash(grep *), Read, Write
---

# git-timeline

Multi-stage pipeline that turns a git repository into a rich HTML retrospective. Every stage caches to SQLite so reruns cost $0.

## When to run

User says something like:
- "analyze this repo's history"
- "generate a git timeline for /path/to/repo"
- "run git-timeline on \<repo\>"
- "make me a year-in-review for \<project\>"

## Two modes (pick one)

The five LLM stages (bootstrap, commits, months, synthesize, tree) can run two ways. The skill picks automatically based on the environment; you can force either.

| | **API mode** (preferred, faster) | **In-session mode** (cheaper, simpler) |
|---|---|---|
| Who does the inference | Anthropic API, fully automated | **You**, the Claude Code agent running this skill |
| Setup | `ANTHROPIC_API_KEY` + `pip install anthropic` | nothing — works out of the box |
| Cost | API tokens (~$2.75 for ~860 commits) | $0 API spend (uses this Claude Code session) |
| Speed | fast, unattended | slower; you answer batched prompts per stage |
| Selected when | `ANTHROPIC_API_KEY` is set | no key set, or `GIT_TIMELINE_INSESSION=1` |

Force a mode with `GIT_TIMELINE_INSESSION=1` (in-session) or `=0` (API; errors loudly if no key). When unsure which the user wants: if a key is available, use API mode; otherwise use in-session — don't ask them to obtain a key.

## Prerequisites (verify before running)

1. **Python 3.11+** — `python3 --version`
2. **For API mode only**: the `anthropic` SDK (`python3 -c "import anthropic"`, install with `pip install -r requirements.txt`) and `ANTHROPIC_API_KEY` in env. Do NOT paste the key value into chat. In-session mode needs neither.
3. Target path is a valid git repo — `ls <path>/.git` should succeed.

## The pipeline

Run these steps **from the skill directory** so the `git_timeline` package is importable. The canonical invocation is:

```bash
cd ~/.claude/skills/git-timeline && python3 -m git_timeline.<stage> <repo_path> [flags]
```

Data (SQLite + HTML) lands at `~/.git-timeline/` by default. Override with `GIT_TIMELINE_HOME=/path`.

| # | Stage         | Command                                          | Model       | Typical cost | Notes |
|---|---------------|--------------------------------------------------|-------------|--------------|-------|
| 1 | Extract       | `python3 -m git_timeline.extract <repo> --ref main` | —           | $0           | Pulls git log into SQLite. Use `--ref HEAD` if branch name unknown. |
| 2 | Preview       | `python3 -m git_timeline.preview --db <db>`       | —           | $0           | Offline cost estimate. Show to user before proceeding. |
| 3 | Bootstrap     | `python3 -m git_timeline.bootstrap <repo>`        | Sonnet 4.6  | ~$0.04       | Reads README, manifests, tree → project anchor. |
| 4 | Commits       | `python3 -m git_timeline.commits <repo> --budget 15` | Haiku 4.5   | ~$0.002/commit | Per-commit summaries. `--budget` caps spend. `--limit N` for a sample first. |
| 5 | Survival      | `python3 -m git_timeline.survival <repo>`         | —           | $0           | Line-level blame analysis: how much of each month's code survived to HEAD. |
| 5b| Survival curve| `python3 -m git_timeline.survival_curve <repo>`   | —           | $0 (slow)    | Cohort/Kaplan-Meier code survival: blames a monthly snapshot tree each, yields a survival curve S(age) + a **code half-life**. Compute-heavy (N blames); cached. `--max-snapshots N`, `--workers N`. |
| 6 | Months        | `python3 -m git_timeline.months <repo>`           | Sonnet 4.6  | ~$0.02/month | Monthly theme rollups. |
| 7 | Synthesize    | `python3 -m git_timeline.synthesize <repo>`       | Opus 4.8    | ~$0.30       | One-page hindsight timeline. |
| 8 | Tree          | `python3 -m git_timeline.tree <repo>`             | Sonnet 4.6  | ~$0.09       | Tech-tree DAG (threads + dead ends). |
| 9 | Render        | `python3 -m git_timeline.render`                  | —           | $0           | Emits HTML site for ALL analyzed repos. Timeline bars are √(LoC added) × survival; adds code-survival-curve + half-life if stage 5b ran. |

Model/cost columns above are for **API mode**. In **in-session mode** every LLM stage costs $0 API spend and instead pauses for you to answer prompts (see below).

## Default playbook

When the user says "analyze \<repo\>":

1. **Confirm prerequisites** and **decide the mode** (API if a key is set, else in-session).
2. **Run stages 1–2** to extract + preview. In API mode, report the cost estimate.
3. **For small repos (<300 commits)**: run 3–9 in sequence, no confirmation needed.
4. **For larger repos**: after stage 2, show the estimate, ask for confirmation before stage 4 (commits) since that's the bulk of the spend (API) or the bulk of the prompts you'll answer (in-session). Offer `--limit 100` as a sample first.
5. **Between stages**: in API mode report running spend and pass `--budget 15` to stage 4 as a ceiling.
6. **Finish**: `open ~/.git-timeline/output/site/<repo_name>/index.html` (macOS) or print the path.

## In-session playbook (no API key)

In in-session mode the LLM stages can't call out, so each one runs a quick two-phase hand-off. **You** are the model. For each LLM stage (bootstrap, commits, months, synthesize, tree):

1. **Run the stage.** If it exits with code **3** and prints `[in-session] N prompt(s) need answers`, it has written a prompts file at `~/.git-timeline/insession/<repo>.<stage>.prompts.jsonl`.
2. **Read that file.** Each line is a JSON object: `{key, model, max_tokens, system, user}`. The `system` field is the full instruction (output format, JSON schema, etc.); `user` is the input to analyze.
3. **Answer every prompt** exactly as the described model would — follow the `system` instructions to the letter (most stages demand strict JSON with no prose/fences; synthesize wants markdown).
4. **Write the answers file** at `~/.git-timeline/insession/<repo>.<stage>.answers.jsonl` — one JSON object per line: `{"key": "<same key from the prompt>", "response": "<your output as a string>"}`. For JSON-output stages, `response` is the JSON encoded as a string.
5. **Re-run the exact same stage.** It auto-ingests your answers into the cache, then completes. Both hand-off files are deleted on success.

Notes:
- **Big repos**: the commits stage may queue hundreds of prompts. You can answer in batches — write some answers, re-run (it persists those and re-queues only the rest), repeat until it exits 0. Never silently skip commits; if you cap, say so.
- Answers are cached by content hash like everything else, so a re-run after answering is free and idempotent.
- If you accidentally re-run before answering, it just re-emits the same prompts file — harmless.

## Re-running

Safe to re-run any stage. The content-hash cache means unchanged inputs cost $0. Prompt changes *do* invalidate cache — this is intentional (iterate freely on wording, only byte-identical inputs hit cache).

## Key flags users may ask about

- `--ref <branch>`: which branch to walk (default HEAD; often `main` is what they want).
- `--limit N`: stage 4 only; first N unresolved commits.
- `--budget <dollars>`: stage 4 hard cap.
- `--db <path>`: override SQLite location.
- `GIT_TIMELINE_HOME=<path>`: override data root.
- `GIT_TIMELINE_INSESSION=1|0`: force in-session (1) or API (0) mode, overriding key auto-detection.

## What gets generated

- `~/.git-timeline/cache/<repo>.db` — SQLite with all extracted and LLM-produced data.
- `~/.git-timeline/output/site/<repo>/index.html` — per-repo report (hindsight timeline, tech-tree DAG, month detail).
- `~/.git-timeline/output/site/<repo>/wrapped.html` — Spotify-Wrapped-style story experience.
- `~/.git-timeline/output/site/index.html` — multi-repo dashboard. Each analyzed repo appears automatically.

## Troubleshooting

- **"No bootstrap anchor found"** — stage 3 wasn't run or didn't persist. Re-run bootstrap.
- **A stage exits with code 3** — that's not an error; it's in-session mode asking for answers. Follow the in-session playbook above.
- **`anthropic` import error / "api_key must be set"** — you're in API mode without a key. Set `ANTHROPIC_API_KEY`, or unset it (and any `GIT_TIMELINE_INSESSION=0`) to fall back to in-session mode.
- **`temperature is deprecated` error** — Opus rejects temperature; already handled in `synthesize.py` (`temperature=None`).
- **`git blame` slow on large repos** — expected; stage 5 processes every file at HEAD. First-time cost is linear in file count.
- **Missing `.db`** — `extract.py` hasn't run yet, or `GIT_TIMELINE_HOME` is set differently than expected.

## Pipeline order (dependencies)

```
extract  →  preview (optional)
extract  →  bootstrap  →  commits  →  months  →  synthesize ─┐
extract  →  survival                                          ├→  tree  →  render
extract  →  survival_curve (optional, slow)                   │
                                                  months  ────┘
```

Survival and survival_curve are standalone (need only the commits table + the repo on disk). survival_curve is the slowest stage (one full-tree `git blame` per monthly snapshot) and is optional — skip it for a quick run; the report just omits the survival-curve section and half-life stat. Render reads whatever is present and degrades gracefully if a stage is missing.
