"""Write a stream of Entry objects into the nodes table, linking each to its parent.

Entries must arrive parents-before-children (both scanners guarantee pre-order). Files smaller than
config.MIN_FILE_BYTES are skipped; their bytes are already included in their folder's totals.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable

from .. import config
from . import Entry

BATCH = 20_000


def _split(path: str) -> tuple[str, str]:
    """('C:\\Users\\x', 'x'); drive roots return ('', 'C:\\')."""
    if len(path) <= 3:
        return "", path
    i = path.rfind("\\")
    parent = path[:i] if i > 2 else path[:3]
    return parent, path[i + 1:]


def import_entries(con: sqlite3.Connection, scan_id: int, letter: str, entries: Iterable[Entry],
                   min_file_bytes: int = config.MIN_FILE_BYTES, on_progress=None) -> dict:
    ids: dict[str, int] = {}
    next_id = (con.execute("SELECT COALESCE(MAX(id),0) FROM nodes").fetchone()[0] or 0) + 1
    rows: list[tuple] = []
    n_dirs = n_files = n_skipped = 0
    t0 = time.time()

    def flush():
        nonlocal rows
        if rows:
            con.executemany(
                "INSERT INTO nodes(id,scan_id,parent_id,drive,path,name,is_dir,depth,size,allocated,mtime,attrs,files,folders)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            rows = []

    con.execute("BEGIN")
    try:
        for e in entries:
            if not e.is_dir and e.size < min_file_bytes:
                n_skipped += 1
                continue
            parent, name = _split(e.path)
            parent_id = ids.get(parent) if parent else None
            depth = 0 if not parent else e.path.count("\\") - (1 if e.path.endswith("\\") else 0)
            nid = next_id
            next_id += 1
            if e.is_dir:
                ids[e.path] = nid
                n_dirs += 1
            else:
                n_files += 1
            rows.append((nid, scan_id, parent_id, letter, e.path, name, int(e.is_dir), depth,
                         e.size, e.allocated, e.mtime, e.attrs, e.files, e.folders))
            if len(rows) >= BATCH:
                flush()
                if on_progress:
                    on_progress(n_dirs + n_files)
        flush()
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return {"dirs": n_dirs, "files": n_files, "skipped_small_files": n_skipped, "seconds": round(time.time() - t0, 1)}
