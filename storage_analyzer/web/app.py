"""FastAPI server: JSON API over the SQLite tables + the single-page UI in static/.

Runs as the normal user. Long jobs (scan, AI) run in a background thread; the UI polls /api/jobs.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import actions, analysis, config, db
from ..ai import claude_code
from ..rules import load_rules
from ..scanner import list_drives

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Storage Analyzer")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_lock = threading.Lock()
_job: dict = {"running": False, "kind": None, "log": [], "error": None, "started": None, "finished": None,
              "progress": None, "result": None}


def _job_progress(done: int, total: int, label: str | None = None):
    _job["progress"] = {"done": done, "total": total, "label": label}


def _con():
    return db.connect()


def _scan_id(con) -> int:
    sid = db.latest_scan_id(con)
    if sid is None:
        raise HTTPException(404, "No completed scan yet. Click Rescan.")
    return sid


def _job_log(msg: str, end: str = "\n"):
    if end == "\r":
        if _job["log"] and _job["log"][-1].startswith(msg.split(":")[0]):
            _job["log"][-1] = msg
            return
    _job["log"].append(msg)


def _run_job(kind: str, fn):
    with _lock:
        if _job["running"]:
            raise HTTPException(409, f"A {_job['kind']} job is already running")
        _job.update(running=True, kind=kind, log=[], error=None, started=datetime.now().isoformat(timespec="seconds"),
                    finished=None, progress=None, result=None)

    def body():
        try:
            fn()
        except Exception as ex:  # noqa: BLE001
            _job["error"] = str(ex)
            _job["log"].append(f"ERROR: {ex}")
        finally:
            _job["running"] = False
            _job["finished"] = datetime.now().isoformat(timespec="seconds")

    threading.Thread(target=body, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/jobs")
def jobs():
    return _job


@app.get("/api/state")
def state():
    con = _con()
    sid = db.latest_scan_id(con)
    out = {"scan": None, "drives": [], "totals": {}, "ai_available": claude_code.is_available(),
           "ai_enabled": db.get_setting(con, "ai_enabled", "1") == "1", "job": _job,
           "wiztree": config.WIZTREE_EXE.exists(), "stale_days": config.STALE_DAYS}
    if sid is None:
        return out
    s = con.execute("SELECT * FROM scans WHERE id=?", (sid,)).fetchone()
    out["scan"] = dict(s)
    out["scan"]["nodes"] = con.execute("SELECT COUNT(*) FROM nodes WHERE scan_id=?", (sid,)).fetchone()[0]
    for d in con.execute("SELECT * FROM drives WHERE scan_id=? ORDER BY letter", (sid,)):
        dd = dict(d)
        dd["totals"] = analysis.totals_by_safety(con, sid, d["letter"])
        out["drives"].append(dd)
    out["totals"] = analysis.totals_by_safety(con, sid)
    out["categories"] = {k: v for k, v in load_rules().categories.items()}
    p = con.execute("SELECT git_state, COUNT(*) n, SUM(rebuildable_bytes) b FROM projects WHERE scan_id=? GROUP BY git_state",
                    (sid,)).fetchall()
    out["projects"] = {r["git_state"]: {"n": r["n"], "bytes": r["b"] or 0} for r in p}
    out["stale"] = dict(con.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(rebuildable_bytes),0) bytes FROM projects WHERE scan_id=? AND stale_days>=?",
        (sid, config.STALE_DAYS)).fetchone())
    out["unknown_count"] = con.execute("SELECT COUNT(*) FROM classifications WHERE scan_id=? AND safety='unknown'",
                                       (sid,)).fetchone()[0]
    return out


@app.get("/api/items")
def items(safety: str | None = None, category: str | None = None, drive: str | None = None,
          min_bytes: int = 0, limit: int = 200, offset: int = 0, q: str | None = None, order: str = "excl_bytes"):
    con = _con()
    sid = _scan_id(con)
    rows = analysis.items(con, sid, safety=safety, category=category, drive=drive, min_bytes=min_bytes,
                          limit=limit if not q else 5000, offset=0 if q else offset, order=order)
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r["path"].lower()][offset:offset + limit]
    return rows


@app.get("/api/categories")
def categories(drive: str | None = None):
    con = _con()
    sid = _scan_id(con)
    return analysis.totals_by_category(con, sid, drive)


@app.get("/api/projects")
def projects(drive: str | None = None):
    con = _con()
    sid = _scan_id(con)
    q = "SELECT p.*, n.drive FROM projects p JOIN nodes n ON n.id=p.node_id WHERE p.scan_id=? "
    args: list = [sid]
    if drive:
        q += "AND n.drive=? "
        args.append(drive)
    q += "ORDER BY p.rebuildable_bytes DESC, p.total_bytes DESC"
    out = []
    for r in con.execute(q, args):
        d = dict(r)
        d["types"] = json.loads(d["types"] or "[]")
        d["git_info"] = json.loads(d["git_info"] or "{}")
        d["stale"] = d["stale_days"] is not None and d["stale_days"] >= config.STALE_DAYS
        lo, hi = d["path"] + "\\", d["path"] + "]"
        d["rebuildable"] = [dict(k) for k in con.execute(
            analysis.ITEM_SQL + "AND c.category='dev' AND n.path>? AND n.path<? ORDER BY n.allocated DESC", (sid, lo, hi))]
        out.append(d)
    return out


def _effective(con, sid: int, node) -> dict | None:
    """Nearest classification at or above a node (for the tree view)."""
    cut = node["path"]
    while True:
        r = con.execute("SELECT c.safety, c.category, c.source, c.reason, c.action, c.node_id FROM classifications c "
                        "JOIN nodes n ON n.id=c.node_id WHERE c.scan_id=? AND n.path=?", (sid, cut)).fetchone()
        if r:
            return dict(r)
        if len(cut) <= 3:
            return None
        cut = cut[:cut.rfind("\\")]
        if len(cut) == 2:
            cut += "\\"


def _children(con, sid: int, node, limit: int) -> tuple[list[dict], dict]:
    kids = con.execute("SELECT * FROM nodes WHERE scan_id=? AND parent_id=? ORDER BY allocated DESC", (sid, node["id"])).fetchall()
    shown, rest = kids[:limit], kids[limit:]
    out = []
    for k in shown:
        eff = _effective(con, sid, k) or {}
        out.append({"id": k["id"], "name": k["name"], "path": k["path"], "is_dir": k["is_dir"],
                    "allocated": k["allocated"], "mtime": k["mtime"], "files": k["files"], "folders": k["folders"],
                    "safety": eff.get("safety", "none"), "category": eff.get("category"), "source": eff.get("source"),
                    "own": eff.get("node_id") == k["id"]})
    return out, {"count": len(rest), "allocated": sum(r["allocated"] for r in rest)}


@app.get("/api/tree")
def tree(node_id: int | None = None, path: str | None = None, drive: str | None = None, limit: int = 60,
         descend: bool = True, nested: int = 12):
    """Children of a node (or a path, or the drive root) with effective safety, for the explore map.
    descend: skip through pass-through folders (one child holding >= 95%), e.g. C:\\Users -> C:\\Users\\chamu.
    nested: for the largest folders also return their own children (second level of the map)."""
    con = _con()
    sid = _scan_id(con)
    if path:
        node = con.execute("SELECT * FROM nodes WHERE scan_id=? AND path=? COLLATE NOCASE", (sid, path.rstrip("\\") or path)).fetchone()
    elif node_id is None:
        drive = (drive or "C").upper().rstrip(":")
        node = con.execute("SELECT * FROM nodes WHERE scan_id=? AND path=?", (sid, drive + ":\\")).fetchone()
    else:
        node = con.execute("SELECT * FROM nodes WHERE scan_id=? AND id=?", (sid, node_id)).fetchone()
    if not node:
        raise HTTPException(404, "node not found in the current scan (the page may be showing an older scan: reload)")
    out_kids, rest = _children(con, sid, node, limit)
    hops = 0
    while descend and hops < 4 and out_kids and out_kids[0]["is_dir"] and node["allocated"] \
            and out_kids[0]["allocated"] >= 0.95 * node["allocated"] and len(out_kids) + rest["count"] <= 8:
        node = con.execute("SELECT * FROM nodes WHERE id=?", (out_kids[0]["id"],)).fetchone()
        out_kids, rest = _children(con, sid, node, limit)
        hops += 1
    if nested:
        total = node["allocated"] or 1
        for k in out_kids:
            if k["is_dir"] and k["allocated"] >= 0.03 * total:
                sub = con.execute("SELECT * FROM nodes WHERE id=?", (k["id"],)).fetchone()
                k["children"], k["rest"] = _children(con, sid, sub, nested)
    # ancestors for breadcrumbs
    crumbs = []
    cur = node
    while cur is not None:
        crumbs.append({"id": cur["id"], "name": cur["name"], "path": cur["path"]})
        cur = con.execute("SELECT * FROM nodes WHERE id=?", (cur["parent_id"],)).fetchone() if cur["parent_id"] else None
    crumbs.reverse()
    return {"node": {"id": node["id"], "path": node["path"], "name": node["name"], "allocated": node["allocated"]},
            "crumbs": crumbs, "children": out_kids,
            "rest": rest}


@app.get("/api/node/{node_id}")
def node_detail(node_id: int):
    con = _con()
    sid = _scan_id(con)
    n = con.execute("SELECT * FROM nodes WHERE scan_id=? AND id=?", (sid, node_id)).fetchone()
    if not n:
        raise HTTPException(404, "node not found")
    d = dict(n)
    d["classification"] = _effective(con, sid, n)
    c = con.execute("SELECT * FROM classifications WHERE scan_id=? AND node_id=?", (sid, node_id)).fetchone()
    d["own"] = dict(c) if c else None
    d["protected"] = actions.is_protected(con, sid, n["path"])
    ai = con.execute("SELECT * FROM ai_cache WHERE path=?", (n["path"].lower(),)).fetchone()
    d["ai"] = json.loads(ai["response"]) if ai else None
    lo, hi = n["path"] + "\\", n["path"] + "]"
    d["nested"] = [dict(r) for r in con.execute(
        analysis.ITEM_SQL + "AND n.path>? AND n.path<? ORDER BY c.excl_bytes DESC LIMIT 20", (sid, lo, hi))]
    return d


class Paths(BaseModel):
    paths: list[str]


@app.post("/api/resolve")
def resolve(body: Paths):
    """Map paths to node ids in the current scan (selection survives rescans this way)."""
    con = _con()
    sid = _scan_id(con)
    out = {}
    for p in body.paths[:10000]:
        r = con.execute("SELECT id FROM nodes WHERE scan_id=? AND path=?", (sid, p)).fetchone()
        if r:
            out[p] = r["id"]
    return {"scan_id": sid, "ids": out}


class Ids(BaseModel):
    node_ids: list[int]
    dry_run: bool = True


@app.post("/api/cleanup/plan")
def cleanup_plan(body: Ids):
    con = _con()
    sid = _scan_id(con)
    return [a.__dict__ for a in actions.plan(con, sid, body.node_ids)]


@app.post("/api/cleanup/run")
def cleanup_run(body: Ids):
    """Dry runs answer immediately. Real cleanups run as a background job (watch /api/jobs; results land in
    /api/log), because moving hundreds of folders and running commands can take minutes."""
    con = _con()
    sid = _scan_id(con)
    acts = actions.plan(con, sid, body.node_ids)
    if body.dry_run:
        res = actions.execute(con, acts, dry_run=True)
        return {"actions": [a.__dict__ for a in res], "freed": 0, "dry_run": True, "started": False}

    def job():
        c = db.connect()
        res = actions.execute(c, acts, dry_run=False, log=_job_log,
                              progress=lambda d, t: _job_progress(d, t, "items processed"))
        _job["result"] = {"ok": sum(1 for a in res if a.result == "ok"), "failed": sum(1 for a in res if a.result == "failed"),
                          "skipped": sum(1 for a in res if a.result == "skipped"),
                          "freed": sum(a.bytes for a in res if a.result == "ok"),
                          "ok_ids": [a.node_id for a in res if a.result == "ok"]}
    _job["result"] = None
    _run_job("cleanup", job)
    return {"started": True, "dry_run": False, "count": sum(1 for a in acts if not a.refused)}


@app.get("/api/log")
def cleanup_log(limit: int = 200):
    con = _con()
    return [dict(r) for r in con.execute("SELECT * FROM cleanup_log ORDER BY id DESC LIMIT ?", (limit,))]


class AiReq(BaseModel):
    node_ids: list[int] | None = None
    limit: int | None = None
    model: str | None = None


@app.post("/api/ai/run")
def ai_run(body: AiReq):
    con = _con()
    sid = _scan_id(con)
    if db.get_setting(con, "ai_enabled", "1") != "1":
        raise HTTPException(400, "AI helper is switched off")
    if not claude_code.is_available():
        raise HTTPException(400, "Claude Code CLI (`claude`) not found on PATH")

    def job():
        c = db.connect()
        res = claude_code.run(c, sid, model=body.model or claude_code.MODEL, limit=body.limit,
                              node_ids=body.node_ids, log=_job_log,
                              progress=lambda d, t: _job_progress(d, t, "folders sent to Claude"))
        _job["log"].append(f"done: {res}")
    _run_job("ai", job)
    return {"started": True}


class ScanReq(BaseModel):
    drives: list[str] | None = None
    admin: bool = True


@app.post("/api/scan")
def scan_start(body: ScanReq):
    def job():
        from ..scan import run_scan
        c = db.connect()
        _job_progress(0, 3, "step 1/3: scanning")
        sid = run_scan(c, body.drives, admin=body.admin, log=_job_log)
        _job_progress(1, 3, "step 2/3: classifying")
        analysis.run_classify(c, sid, log=_job_log)
        _job_progress(2, 3, "step 3/3: AI helper")
        if db.get_setting(c, "ai_enabled", "1") == "1" and claude_code.is_available():
            _job_log("Asking the AI helper about unknown folders (cached answers reused)...")
            claude_code.run(c, sid, log=_job_log, progress=lambda d, t: _job_progress(d, t, "step 3/3: folders sent to Claude"))
        _job_progress(3, 3, "done")
    _run_job("scan", job)
    return {"started": True}


@app.post("/api/classify")
def classify_now():
    def job():
        c = db.connect()
        analysis.run_classify(c, log=_job_log)
    _run_job("classify", job)
    return {"started": True}


class Settings(BaseModel):
    ai_enabled: bool | None = None


@app.post("/api/settings")
def settings(body: Settings):
    con = _con()
    if body.ai_enabled is not None:
        db.set_setting(con, "ai_enabled", "1" if body.ai_enabled else "0")
    return {"ai_enabled": db.get_setting(con, "ai_enabled", "1") == "1"}


class RuleReq(BaseModel):
    rule: dict


@app.post("/api/rules/accept")
def accept_rule(body: RuleReq):
    r = body.rule
    for k in ("id", "match", "category", "safety"):
        if k not in r:
            raise HTTPException(400, f"rule is missing {k}")
    if r["safety"] == "safe":
        r["safety"] = "review"  # AI-proposed rules never auto-delete until you edit them yourself
    config.USER_RULES.parent.mkdir(exist_ok=True)
    doc = {}
    if config.USER_RULES.exists():
        doc = yaml.safe_load(config.USER_RULES.read_text(encoding="utf-8")) or {}
    rules = doc.setdefault("rules", [])
    if any(x.get("id") == r["id"] for x in rules):
        raise HTTPException(409, f"rule id {r['id']} already exists in user.yaml")
    rules.append(r)
    config.USER_RULES.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return {"ok": True, "count": len(rules)}


@app.post("/api/open")
def open_in_explorer(body: dict):
    path = body.get("path", "")
    if not os.path.exists(path):
        raise HTTPException(404, "path does not exist")
    subprocess.Popen(["explorer", "/select,", path] if os.path.isfile(path) else ["explorer", path])
    return {"ok": True}


@app.get("/api/drives")
def drives():
    return [d.__dict__ | {"used": d.used} for d in list_drives(fixed_only=False)]


@app.exception_handler(Exception)
async def _err(_, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": str(exc)})
