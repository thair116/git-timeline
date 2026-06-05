"""SQLite schema for git-timeline.

One database per analyzed repo, stored at cache/<repo_name>.db.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS commits (
    sha           TEXT PRIMARY KEY,
    committed_at  TEXT NOT NULL,
    author_name   TEXT NOT NULL,
    author_email  TEXT NOT NULL,
    parents       TEXT NOT NULL,
    is_merge      INTEGER NOT NULL,
    subject       TEXT NOT NULL,
    body          TEXT,
    files_changed INTEGER NOT NULL,
    insertions    INTEGER NOT NULL,
    deletions     INTEGER NOT NULL,
    month         TEXT NOT NULL GENERATED ALWAYS AS (substr(committed_at, 1, 7)) STORED
);

CREATE INDEX IF NOT EXISTS idx_commits_month ON commits(month);
CREATE INDEX IF NOT EXISTS idx_commits_date  ON commits(committed_at);

CREATE TABLE IF NOT EXISTS commit_files (
    sha         TEXT NOT NULL,
    path        TEXT NOT NULL,
    insertions  INTEGER NOT NULL,
    deletions   INTEGER NOT NULL,
    PRIMARY KEY (sha, path),
    FOREIGN KEY (sha) REFERENCES commits(sha)
);

CREATE INDEX IF NOT EXISTS idx_commit_files_sha ON commit_files(sha);

-- Cache of LLM outputs, keyed by (stage, input_hash) so prompt changes
-- invalidate the cache automatically.
CREATE TABLE IF NOT EXISTS llm_cache (
    stage       TEXT NOT NULL,
    input_hash  TEXT NOT NULL,
    key         TEXT NOT NULL,         -- e.g. sha or 'YYYY-MM'
    model       TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    response    TEXT NOT NULL,         -- JSON
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (stage, input_hash)
);

CREATE INDEX IF NOT EXISTS idx_llm_cache_key ON llm_cache(stage, key);

-- Per-commit summaries (materialized view of llm_cache for stage='commit').
CREATE TABLE IF NOT EXISTS commit_summaries (
    sha         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,         -- feature|fix|refactor|infra|docs|chore|merge|skipped
    one_liner   TEXT NOT NULL,
    signals     TEXT,                  -- JSON array of tags
    method      TEXT NOT NULL,         -- 'rule' or 'llm:<model>'
    FOREIGN KEY (sha) REFERENCES commits(sha)
);

CREATE TABLE IF NOT EXISTS month_summaries (
    month       TEXT PRIMARY KEY,      -- YYYY-MM
    theme       TEXT NOT NULL,
    shipped     TEXT,                  -- JSON
    abandoned   TEXT,                  -- JSON
    commit_count INTEGER NOT NULL,
    churn_ratio REAL,
    method      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Tech-tree DAG: threads/subsystems as nodes, evolution relationships as edges.
CREATE TABLE IF NOT EXISTS tree_nodes (
    id           TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    category     TEXT,
    start_month  TEXT,
    end_month    TEXT,
    status       TEXT NOT NULL,              -- live | superseded | dead
    description  TEXT
);

CREATE TABLE IF NOT EXISTS tree_edges (
    src  TEXT NOT NULL,
    dst  TEXT NOT NULL,
    kind TEXT NOT NULL,                      -- evolved-into | replaced-by | enabled | branched-from
    PRIMARY KEY (src, dst, kind)
);
"""


def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None
