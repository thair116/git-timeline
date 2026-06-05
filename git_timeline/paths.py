"""Centralized path resolution for git-timeline.

Data (SQLite caches + generated HTML) lives at `~/.git-timeline/` by default
so it survives skill updates and stays out of the skill directory. Override
with GIT_TIMELINE_HOME env var.
"""
from __future__ import annotations

import os
from pathlib import Path


def data_home() -> Path:
    override = os.environ.get("GIT_TIMELINE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".git-timeline").resolve()


def cache_dir() -> Path:
    d = data_home() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_dir() -> Path:
    d = data_home() / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


def site_dir() -> Path:
    d = output_dir() / "site"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path_for(repo_name: str) -> Path:
    return cache_dir() / f"{repo_name}.db"


def insession_dir() -> Path:
    """Where in-session prompt/answer hand-off files live."""
    d = data_home() / "insession"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pending_path(repo_name: str, stage: str) -> Path:
    """Prompts the in-session agent needs to answer for a stage."""
    return insession_dir() / f"{repo_name}.{stage}.prompts.jsonl"


def answers_path(repo_name: str, stage: str) -> Path:
    """Answers the in-session agent writes back for a stage."""
    return insession_dir() / f"{repo_name}.{stage}.answers.jsonl"
