"""SQLite storage for the session index.

The DB is a *pure disposable cache*: everything in it is derived from the
``.jsonl`` session files under ~/.claude/projects and can be rebuilt at any
time. It holds nothing the user would miss.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_PROJECTS_DIR = Path(os.path.expanduser("~/.claude/projects"))
DEFAULT_DB_PATH = Path(os.path.expanduser("~/.claude/ccs.db"))

# Bump on any schema change → connect() auto-drops & rebuilds the cache.
SCHEMA_VERSION = "3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,   -- session id (jsonl filename stem)
    file_path     TEXT NOT NULL,
    project_dir   TEXT,               -- slug directory name
    cwd           TEXT,               -- real working directory
    project       TEXT,               -- display name (basename of cwd)
    first_ts      TEXT,               -- ISO timestamp of first message
    last_ts       TEXT,               -- ISO timestamp of last message
    duration_s    INTEGER,
    turns         INTEGER,            -- human prompt count
    assistant_turns INTEGER,
    tool_calls    INTEGER,
    models        TEXT,               -- comma-separated distinct models
    git_branches  TEXT,               -- comma-separated distinct branches
    branch        TEXT,               -- current/last-seen git branch
    forked_from   TEXT,               -- parent session id (conversation branch)
    custom_title  TEXT,
    first_prompt  TEXT,
    last_prompt   TEXT,
    entrypoint    TEXT,
    pr_links      TEXT,
    out_tokens    INTEGER,
    size          INTEGER,            -- file size in bytes
    mtime         REAL                -- file mtime (index staleness key)
);

CREATE INDEX IF NOT EXISTS idx_sessions_last_ts ON sessions(last_ts);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    session_id UNINDEXED,
    role       UNINDEXED,
    ts         UNINDEXED,
    text,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

SESSION_COLUMNS = [
    "id", "file_path", "project_dir", "cwd", "project", "first_ts", "last_ts",
    "duration_s", "turns", "assistant_turns", "tool_calls", "models",
    "git_branches", "branch", "forked_from", "custom_title", "first_prompt",
    "last_prompt",
    "entrypoint", "pr_links", "out_tokens", "size", "mtime",
]


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # If the schema version differs (or is absent), the derived cache is stale —
    # drop everything and let the next index rebuild it from the transcripts.
    ver = None
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone():
        row = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        ver = row[0] if row else None
    if ver != SCHEMA_VERSION:
        for t in ("sessions", "messages_fts", "meta"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO meta (k, v) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()
    return conn


def upsert_session(conn: sqlite3.Connection, row: dict) -> None:
    cols = ", ".join(SESSION_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in SESSION_COLUMNS)
    conn.execute(
        f"INSERT OR REPLACE INTO sessions ({cols}) VALUES ({placeholders})",
        {c: row.get(c) for c in SESSION_COLUMNS},
    )


def replace_fts(conn: sqlite3.Connection, session_id: str, messages: list[dict]) -> None:
    conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (session_id,))
    if messages:
        conn.executemany(
            "INSERT INTO messages_fts (session_id, role, ts, text) VALUES (?, ?, ?, ?)",
            [(session_id, m["role"], m.get("ts"), m["text"]) for m in messages],
        )


def delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (session_id,))


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    r = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return r["v"] if r else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, str(value)))
