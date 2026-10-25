"""SQLite storage for scans, the folder tree, classifications, projects, the AI cache and the cleanup log."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    scanner     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',   -- running | done | failed
    note        TEXT
);

CREATE TABLE IF NOT EXISTS drives (
    scan_id   INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    letter    TEXT NOT NULL,          -- 'C'
    label     TEXT,
    fs        TEXT,
    capacity  INTEGER,
    free      INTEGER,
    used      INTEGER,
    PRIMARY KEY (scan_id, letter)
);

-- One row per folder, plus files >= config.MIN_FILE_BYTES.
CREATE TABLE IF NOT EXISTS nodes (
    id         INTEGER PRIMARY KEY,
    scan_id    INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    parent_id  INTEGER,
    drive      TEXT NOT NULL,
    path       TEXT NOT NULL,         -- full path, folders WITHOUT trailing backslash (except 'C:\\')
    name       TEXT NOT NULL,
    is_dir     INTEGER NOT NULL,
    depth      INTEGER NOT NULL,
    size       INTEGER NOT NULL,      -- logical bytes (recursive for folders)
    allocated  INTEGER NOT NULL,      -- bytes on disk (recursive for folders)
    mtime      TEXT,                  -- ISO 8601, newest modified time inside for folders
    attrs      INTEGER,
    files      INTEGER,               -- recursive counts (folders only)
    folders    INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS nodes_scan_path ON nodes(scan_id, path);
CREATE INDEX IF NOT EXISTS nodes_scan_parent ON nodes(scan_id, parent_id);
CREATE INDEX IF NOT EXISTS nodes_scan_alloc  ON nodes(scan_id, allocated DESC);

CREATE TABLE IF NOT EXISTS classifications (
    scan_id    INTEGER NOT NULL,
    node_id    INTEGER NOT NULL,
    category   TEXT NOT NULL,
    safety     TEXT NOT NULL,         -- safe | review | keep | unknown
    source     TEXT NOT NULL,         -- rule | ai | user
    rule_id    TEXT,
    confidence REAL,
    reason     TEXT,
    action     TEXT,
    method     TEXT,                  -- recycle | command | manual | none
    command    TEXT,
    admin      INTEGER NOT NULL DEFAULT 0,
    excl_bytes INTEGER NOT NULL DEFAULT 0,  -- allocated minus nested roots: sum these for totals
    created_at TEXT NOT NULL,
    PRIMARY KEY (scan_id, node_id)
);
CREATE INDEX IF NOT EXISTS class_cat ON classifications(scan_id, category);

CREATE TABLE IF NOT EXISTS projects (
    scan_id           INTEGER NOT NULL,
    node_id           INTEGER NOT NULL,
    path              TEXT NOT NULL,
    name              TEXT NOT NULL,
    types             TEXT NOT NULL,  -- JSON list, e.g. ["python","node"]
    last_touched      TEXT,           -- ISO date of newest source file
    stale_days        INTEGER,
    total_bytes       INTEGER,
    rebuildable_bytes INTEGER,
    git_state         TEXT,           -- ok | warn | none | n/a
    git_info          TEXT,           -- JSON
    PRIMARY KEY (scan_id, node_id)
);

-- AI answers are kept across scans; keyed by path + a fingerprint of size/mtime/counts.
CREATE TABLE IF NOT EXISTS ai_cache (
    path        TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    model       TEXT,
    response    TEXT NOT NULL,        -- JSON
    created_at  TEXT NOT NULL,
    PRIMARY KEY (path, fingerprint)
);

CREATE TABLE IF NOT EXISTS cleanup_log (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    path      TEXT NOT NULL,
    bytes     INTEGER,
    method    TEXT NOT NULL,
    command   TEXT,
    result    TEXT NOT NULL,          -- ok | failed | skipped | dry-run
    detail    TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, isolation_level=None)  # autocommit; we manage transactions explicitly
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-200000")  # ~200 MB
    con.executescript(SCHEMA)
    _migrate(con)
    return con


# Columns added after the first release: (table, column, declaration). ALTER TABLE ADD COLUMN is cheap.
_ADDED_COLUMNS = [
    ("classifications", "admin", "INTEGER NOT NULL DEFAULT 0"),
    ("classifications", "excl_bytes", "INTEGER NOT NULL DEFAULT 0"),
]


def _migrate(con: sqlite3.Connection) -> None:
    for table, col, decl in _ADDED_COLUMNS:
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


@contextmanager
def transaction(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    con.execute("BEGIN")
    try:
        yield con
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def latest_scan_id(con: sqlite3.Connection) -> int | None:
    row = con.execute("SELECT id FROM scans WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def get_setting(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
