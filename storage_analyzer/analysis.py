"""Glue: projects → rules → git/staleness; plus the read queries the CLI and web UI share."""

from __future__ import annotations

import sqlite3

from . import db, projects as projects_mod, rules as rules_mod


def run_classify(con: sqlite3.Connection, scan_id: int | None = None, log=print) -> int:
    scan_id = scan_id or db.latest_scan_id(con)
    if scan_id is None:
        raise RuntimeError("No completed scan. Run `storage-analyzer scan` first.")
    rs = rules_mod.load_rules()
    log(f"Classifying scan #{scan_id} with {len(rs.rules)} rules")
    log("Detecting projects...")
    projs = projects_mod.detect(con, scan_id, rs, log=log)
    ptypes = {p.path.lower(): p.types for p in projs}
    log("Applying rules...")
    counts = rules_mod.classify(con, scan_id, rs, ptypes, log=log)
    log("Reading git status and project age...")
    projects_mod.analyse(con, scan_id, projs, log=log)
    log("Checking which folders running programs and tool configs use...")
    n = mark_in_use(con, scan_id)
    if n:
        log(f"  {n} folder(s) are used by a running program or referenced by a tool config: marked review")
    # Re-apply cached AI answers (classification was rebuilt from scratch above). Never calls Claude.
    from .ai import claude_code
    res = claude_code.run(con, scan_id, cache_only=True, log=lambda *a, **k: None)
    if res["cached"]:
        log(f"  re-applied {res['cached']} cached AI answers")
    log("Done: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return scan_id


def mark_in_use(con: sqlite3.Connection, scan_id: int, index=None, refs=None) -> int:
    """Downgrade safe folders to review when a running process runs from them (naming the process) or a tool
    config references them (naming the tool). Review folders get the note added to their reason."""
    from .procs import InUseIndex
    from .refs import RefIndex
    index = index or InUseIndex()
    refs = refs or RefIndex()
    rows = con.execute("SELECT c.node_id, n.path, c.reason, c.safety FROM classifications c JOIN nodes n ON n.id=c.node_id "
                       "WHERE c.scan_id=? AND c.safety IN ('safe','review','rebuildable') AND n.is_dir=1", (scan_id,)).fetchall()
    updates = []
    for r in rows:
        notes = [w for w in (index.describe(r["path"]), refs.describe(r["path"])) if w]
        if notes:
            note = "; ".join(notes) + ". Deleting it breaks that. "
            if not (r["reason"] or "").startswith(note):
                updates.append((note + (r["reason"] or ""), r["node_id"]))
    if updates:
        con.execute("BEGIN")
        con.executemany("UPDATE classifications SET safety=CASE WHEN safety='safe' THEN 'review' ELSE safety END, reason=? "
                        "WHERE scan_id=%d AND node_id=?" % scan_id, updates)
        con.execute("COMMIT")
    return len(updates)


def totals_by_safety(con: sqlite3.Connection, scan_id: int, drive: str | None = None) -> dict[str, int]:
    q = ("SELECT c.safety, SUM(c.excl_bytes) b FROM classifications c JOIN nodes n ON n.id=c.node_id "
         "WHERE c.scan_id=? ")
    args: list = [scan_id]
    if drive:
        q += "AND n.drive=? "
        args.append(drive)
    q += "GROUP BY c.safety"
    return {r["safety"]: r["b"] or 0 for r in con.execute(q, args)}


def totals_by_category(con: sqlite3.Connection, scan_id: int, drive: str | None = None) -> list[dict]:
    q = ("SELECT c.category, c.safety, SUM(c.excl_bytes) b, COUNT(*) n FROM classifications c "
         "JOIN nodes n ON n.id=c.node_id WHERE c.scan_id=? ")
    args: list = [scan_id]
    if drive:
        q += "AND n.drive=? "
        args.append(drive)
    q += "GROUP BY c.category, c.safety"
    out: dict[str, dict] = {}
    for r in con.execute(q, args):
        d = out.setdefault(r["category"], {"category": r["category"], "total": 0, "count": 0,
                                           "safe": 0, "review": 0, "keep": 0, "unknown": 0})
        d["total"] += r["b"] or 0
        d["count"] += r["n"]
        d[r["safety"]] = d.get(r["safety"], 0) + (r["b"] or 0)
    return sorted(out.values(), key=lambda d: -d["total"])


ITEM_SQL = """
SELECT n.id, n.path, n.name, n.drive, n.is_dir, n.allocated, n.size, n.mtime, n.files, n.folders,
       c.category, c.safety, c.source, c.rule_id, c.confidence, c.reason, c.action, c.method, c.command,
       c.admin, c.excl_bytes
FROM classifications c JOIN nodes n ON n.id=c.node_id
WHERE c.scan_id=? """


def items(con: sqlite3.Connection, scan_id: int, *, safety: str | None = None, category: str | None = None,
          drive: str | None = None, min_bytes: int = 0, limit: int = 200, offset: int = 0,
          order: str = "excl_bytes") -> list[dict]:
    q = ITEM_SQL
    args: list = [scan_id]
    if safety:
        q += "AND c.safety=? "
        args.append(safety)
    if category:
        q += "AND c.category=? "
        args.append(category)
    if drive:
        q += "AND n.drive=? "
        args.append(drive)
    if min_bytes:
        q += "AND c.excl_bytes>=? "
        args.append(min_bytes)
    col = {"excl_bytes": "c.excl_bytes", "allocated": "n.allocated", "mtime": "n.mtime", "path": "n.path"}[order]
    q += f"ORDER BY {col} DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    return [dict(r) for r in con.execute(q, args)]
