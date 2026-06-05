# git-timeline

A Claude Code skill that turns a git repository's full history into an
interactive HTML retrospective: a hindsight timeline with line-level
code-survival analysis, a tech-tree DAG of evolving subsystems and dead
ends, and a Spotify-Wrapped-style story experience.

## Install

Clone straight into your Claude Code skills directory:

```bash
# personal (works in every project):
git clone https://github.com/thair116/git-timeline ~/.claude/skills/git-timeline

# or project-local:
git clone https://github.com/thair116/git-timeline <repo>/.claude/skills/git-timeline
```

Then just ask Claude to *"run git-timeline on /path/to/repo."*

It runs in one of two modes — the skill picks automatically:

**API mode** (preferred, faster) — fully automated, costs API tokens:

```bash
pip install -r ~/.claude/skills/git-timeline/requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

**In-session mode** (cheaper, simpler) — no key, no SDK, $0 API spend. When no
`ANTHROPIC_API_KEY` is set, the Claude Code agent running the skill does the LLM
work itself: each stage pauses, writes the prompts it needs answered to a file,
you answer them, and it resumes. Nothing to install. Force it with
`export GIT_TIMELINE_INSESSION=1`.

## Use it

Ask Claude something like:
- *"Run git-timeline on /path/to/repo."*
- *"Analyze the history of this project."*
- *"Make me a year-in-review for my-project."*

Claude will invoke the skill, run the pipeline, and open the result.

## What it does

Nine stages, all cached — reruns cost $0:

1. **extract** — `git log` → SQLite, one row per commit.
2. **preview** — offline cost estimate (shows rule-filtered commits).
3. **bootstrap** — one LLM call reads README + manifests + file tree → project anchor.
4. **commits** — Haiku summarizes every commit (uses the anchor as system prompt, expands to diff for opaque subjects).
5. **survival** — line-level `git blame` on HEAD: how much of each month's code still exists.
6. **months** — Sonnet rolls per-commit summaries into monthly themes.
7. **synthesize** — Opus writes a 1-page hindsight timeline, flagging where narrative and mechanical survival disagree.
8. **tree** — Sonnet builds a tech-tree DAG: 30–50 technical threads with evolution / replacement / death edges.
9. **render** — static HTML site with:
   - per-repo page: hero stats, stack chips, monthly bar chart (√(LoC added) bar height × survival fill), SVG DAG, rendered narrative, month detail cards, analysis cost table
   - a multi-repo dashboard
   - a full-viewport **wrapped** experience with 13 tap-through cards, count-up animations, and a narrative voice that reacts to your data (roasts your cryptic commits, mourns your dead limbs)

## Data locations

- **Code**: `~/.claude/skills/git-timeline/git_timeline/` (or wherever you installed the skill)
- **Cache + site**: `~/.git-timeline/` by default. Override with `GIT_TIMELINE_HOME=/path`.

## Cost

In **API mode**, a 859-commit repo with 14 months of history costs about **$2.75** end-to-end. Almost all of that is per-commit summarization (Haiku, <$0.002/commit). Cache means reruns are free.

In **in-session mode** there's **$0 API spend** — the trade is your time/tokens in the Claude Code session answering batched prompts (one batch per stage; the commits stage is the big one). Best for small-to-mid repos or when you'd rather not set up a key.

## Running it manually

```bash
cd ~/.claude/skills/git-timeline

python3 -m git_timeline.extract    /path/to/repo --ref main
python3 -m git_timeline.bootstrap  /path/to/repo
python3 -m git_timeline.commits    /path/to/repo --budget 15
python3 -m git_timeline.survival   /path/to/repo
python3 -m git_timeline.months     /path/to/repo
python3 -m git_timeline.synthesize /path/to/repo
python3 -m git_timeline.tree       /path/to/repo
python3 -m git_timeline.render

open ~/.git-timeline/output/site/index.html
```

In **in-session mode**, the five LLM stages (bootstrap, commits, months, synthesize, tree) exit with code **3** and write a `*.prompts.jsonl` under `~/.git-timeline/insession/`. Answer them into the matching `*.answers.jsonl`, then re-run that stage to ingest and continue. The agent driving the skill handles this automatically — see SKILL.md.

## Philosophy

- **Cache everything by content hash.** Prompt changes invalidate; inputs the same cost $0.
- **Use the cheapest model that works for each stage.** Haiku for per-commit, Sonnet for rollups, Opus once for final synthesis.
- **Trust mechanical signals over narrative.** The survival ratio disagrees with the LLM's judgment sometimes — that's the most interesting part of the report.
