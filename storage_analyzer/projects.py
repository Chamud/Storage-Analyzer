"""Detect code projects, work out how stale they are, read their git status, and resolve `rebuildable` roots.

Detection sources:
  1. parents of marker *folders* found in the scan (.git, .venv, venv, node_modules, ProjectSettings, .gradle…)
  2. children (depth ≤ 3) of configured `project_roots`, checked on disk for marker *files* (pyproject.toml…)
A candidate inside another project is kept only if it has its own .git (submodule / nested repo).
"""

from __future__ import annotations

import fnmatch
import json
import os
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config
from .rules import RuleSet, glob_to_regex

MARKER_DIRS = {".git": "git", ".venv": "python", "venv": "python", "node_modules": "node",
               "ProjectSettings": "unity", ".idea": None, ".vs": "dotnet"}
EXCLUDE_SEGMENTS = {"node_modules", ".venv", "venv", "site-packages", "library", "appdata", "program files",
                    "program files (x86)", "$recycle.bin", "windows", "programdata", ".git", "packages", "intermediate",
                    ".claude", ".cursor", ".vscode", ".cache", ".gradle", ".nuget", ".m2", ".cargo"}
MIN_PROJECT_DEPTH = 2  # never a drive root, `?:\X` or a user profile (`C:\Users\name` is depth 2)
REBUILDABLE_NAMES = {"node_modules", ".venv", "venv", ".tox", ".nox", "__pycache__", ".pytest_cache", ".mypy_cache",
                     ".ruff_cache", "build", "dist", "out", ".next", ".nuxt", ".turbo", ".parcel-cache", ".cache",
                     "target", ".gradle", "obj", "bin", "debug", "release", "x64", ".vs", "coverage", "library",
                     "temp", "logs", "intermediate", "deriveddatacache", "binaries", "saved", ".git", ".idea"}


@dataclass
class Project:
    node_id: int
    path: str
    name: str
    types: list[str] = field(default_factory=list)
    last_touched: str | None = None
    stale_days: int | None = None
    total_bytes: int = 0
    rebuildable_bytes: int = 0
    git_state: str = "n/a"          # ok | warn | none | n/a
    git_info: dict = field(default_factory=dict)


def subtree_range(path: str) -> tuple[str, str]:
    """Bounds for `path > lo AND path < hi` that select every descendant of `path` using the path index.
    ']' is the character after '\\', so everything starting with 'path\\' sorts between the two."""
    return path + "\\", path + "]"


def _excluded(path: str) -> bool:
    parts = path.lower().split("\\")
    if len(parts) - 1 <= MIN_PROJECT_DEPTH:
        return True
    return any(p in EXCLUDE_SEGMENTS for p in parts[1:])


def _marker_types(path: str, markers: dict) -> list[str]:
    """Check marker files on disk for one folder."""
    try:
        names = os.listdir(path)
    except OSError:
        return []
    lower = {n.lower(): n for n in names}
    types = []
    for t, pats in markers.items():
        for pat in pats:
            if "/" in pat:
                if os.path.exists(os.path.join(path, *pat.split("/"))):
                    types.append(t)
                    break
            elif "*" in pat:
                if any(fnmatch.fnmatch(n, pat.lower()) for n in lower):
                    types.append(t)
                    break
            elif pat.lower() in lower:
                types.append(t)
                break
    return types


def detect(con: sqlite3.Connection, scan_id: int, rs: RuleSet, log=print) -> list[Project]:
    # 1. marker folders from the scan
    q = ("SELECT n.id, n.parent_id, n.name, p.path, p.id FROM nodes n JOIN nodes p ON p.id=n.parent_id "
         "WHERE n.scan_id=? AND n.is_dir=1 AND n.name IN (%s)" % ",".join("?" * len(MARKER_DIRS)))
    cands: dict[str, tuple[int, set[str]]] = {}
    for _, _, name, ppath, pid in con.execute(q, (scan_id, *MARKER_DIRS)):
        if _excluded(ppath):
            continue
        if name == "ProjectSettings" and not os.path.isdir(os.path.join(ppath, "Assets")):
            continue
        entry = cands.setdefault(ppath, (pid, set()))
        if MARKER_DIRS[name]:
            entry[1].add(MARKER_DIRS[name])

    # 2. children of project roots (depth ≤ 3) checked on disk
    root_res = [glob_to_regex(p) for p in rs.project_roots]
    roots = [r for r in con.execute("SELECT id, path, depth FROM nodes WHERE scan_id=? AND is_dir=1 AND depth<=4", (scan_id,))
             if any(rr.match(r["path"]) for rr in root_res)]
    for r in roots:
        lo, hi = subtree_range(r["path"])
        for row in con.execute("SELECT id, path, depth FROM nodes WHERE scan_id=? AND is_dir=1 AND path>? AND path<? AND depth<=?",
                               (scan_id, lo, hi, r["depth"] + 3)):
            if _excluded(row["path"]) or row["path"] in cands:
                continue
            types = _marker_types(row["path"], rs.project_markers)
            if types:
                cands[row["path"]] = (row["id"], set(types))

    # fill in marker-file types for folder-detected candidates too
    projects: list[Project] = []
    for path, (nid, types) in cands.items():
        types |= set(_marker_types(path, rs.project_markers))
        types.discard("git")
        has_git = os.path.isdir(os.path.join(path, ".git"))
        projects.append(Project(nid, path, os.path.basename(path), sorted(types), git_state="n/a" if has_git else "none"))

    # 3. drop nested candidates without their own .git
    by_path = sorted(projects, key=lambda p: len(p.path))
    kept: list[Project] = []
    for p in by_path:
        lp = p.path.lower()
        nested = any(lp.startswith(k.path.lower() + "\\") for k in kept)
        if nested and not os.path.isdir(os.path.join(p.path, ".git")):
            continue
        kept.append(p)
    log(f"  {len(kept)} projects detected")
    return kept


# ----------------------------------------------------------------------------------------------------------

def _git(path: str, *args: str, timeout: int = 30) -> str | None:
    try:
        r = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def git_status(path: str) -> tuple[str, dict]:
    """Returns (state, info). state: ok | warn | none."""
    if not os.path.isdir(os.path.join(path, ".git")):
        return "none", {}
    info: dict = {}
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    info["branch"] = branch
    status = _git(path, "status", "--porcelain", "--untracked-files=normal")
    if status is None:
        return "warn", {"error": "git status failed (locked index or not a repo)"}
    lines = [ln for ln in status.splitlines() if ln.strip()]
    info["uncommitted"] = len(lines)
    info["untracked"] = sum(1 for ln in lines if ln.startswith("??"))
    remotes = (_git(path, "remote") or "").split()
    info["remotes"] = remotes
    unpushed = None
    if remotes:
        up = _git(path, "rev-list", "--count", "@{u}..HEAD")
        if up is not None:
            unpushed = int(up)
            info["upstream"] = _git(path, "rev-parse", "--abbrev-ref", "@{u}")
        else:
            not_on_remote = _git(path, "rev-list", "--count", "HEAD", "--not", "--remotes")
            unpushed = int(not_on_remote) if not_on_remote else None
            info["upstream"] = None
    else:
        total = _git(path, "rev-list", "--count", "HEAD")
        unpushed = int(total) if total else 0
    info["unpushed"] = unpushed
    last = _git(path, "log", "-1", "--format=%cI")
    info["last_commit"] = last
    problems = []
    if info["uncommitted"]:
        problems.append(f"{info['uncommitted']} uncommitted")
    if not remotes:
        problems.append("no remote")
    elif unpushed:
        problems.append(f"{unpushed} unpushed")
    info["summary"] = ", ".join(problems) if problems else "clean, in sync"
    return ("warn" if problems else "ok"), info


def _last_touched(con: sqlite3.Connection, scan_id: int, p: Project) -> str | None:
    """Newest folder mtime inside the project, ignoring rebuildable folders. Cheap and good enough."""
    lo, hi = subtree_range(p.path)
    best = con.execute("SELECT mtime FROM nodes WHERE scan_id=? AND id=?", (scan_id, p.node_id)).fetchone()
    best = best["mtime"] if best else None
    for (path, mtime) in con.execute(
            "SELECT path, mtime FROM nodes WHERE scan_id=? AND is_dir=1 AND path>? AND path<? AND mtime IS NOT NULL",
            (scan_id, lo, hi)):
        rel = path[len(p.path) + 1:].lower().split("\\")
        if any(seg in REBUILDABLE_NAMES for seg in rel):
            continue
        if best is None or mtime > best:
            best = mtime
    return best


def analyse(con: sqlite3.Connection, scan_id: int, projects: list[Project], log=print) -> None:
    """Fill staleness, git status, sizes; resolve rebuildable classifications; write the projects table."""
    now = datetime.now()
    with ThreadPoolExecutor(max_workers=8) as ex:
        git_results = list(ex.map(lambda p: git_status(p.path), projects))
    for p, (state, info) in zip(projects, git_results):
        p.git_state, p.git_info = state, info
        p.last_touched = _last_touched(con, scan_id, p)
        if info.get("last_commit"):
            try:
                lc = datetime.fromisoformat(info["last_commit"]).replace(tzinfo=None).isoformat(timespec="seconds")
                p.last_touched = max(p.last_touched or "", lc)
            except ValueError:
                pass
        if p.last_touched:
            p.stale_days = (now - datetime.fromisoformat(p.last_touched)).days
        row = con.execute("SELECT allocated FROM nodes WHERE id=?", (p.node_id,)).fetchone()
        p.total_bytes = row["allocated"] if row else 0

    # resolve rebuildable roots
    proj_by_lp = {p.path.lower(): p for p in projects}
    rows = con.execute(
        "SELECT c.node_id, n.path, n.allocated, n.mtime, c.reason FROM classifications c JOIN nodes n ON n.id=c.node_id "
        "WHERE c.scan_id=? AND c.safety='rebuildable'", (scan_id,)).fetchall()
    updates = []
    for r in rows:
        lp = r["path"].lower()
        proj = None
        cut = lp
        while "\\" in cut:
            cut = cut[:cut.rfind("\\")]
            if cut in proj_by_lp:
                proj = proj_by_lp[cut]
                break
        if proj is not None:
            proj.rebuildable_bytes += r["allocated"]
            stale = proj.stale_days is not None and proj.stale_days >= config.STALE_DAYS
            if stale:
                safety, extra = "safe", f" Project untouched for {proj.stale_days} days."
            else:
                safety, extra = "review", " Project is active: you would have to rebuild it."
        else:
            age = (now - datetime.fromisoformat(r["mtime"])).days if r["mtime"] else None
            stale = age is not None and age >= config.STALE_DAYS
            safety = "safe" if stale else "review"
            extra = f" Not modified for {age} days." if age is not None else ""
        updates.append((safety, r["reason"] + extra, r["node_id"]))
    con.execute("BEGIN")
    con.executemany("UPDATE classifications SET safety=?, reason=? WHERE scan_id=%d AND node_id=?" % scan_id, updates)
    con.execute("DELETE FROM projects WHERE scan_id=?", (scan_id,))
    con.executemany(
        "INSERT INTO projects(scan_id,node_id,path,name,types,last_touched,stale_days,total_bytes,rebuildable_bytes,"
        "git_state,git_info) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(scan_id, p.node_id, p.path, p.name, json.dumps(p.types), p.last_touched, p.stale_days, p.total_bytes,
          p.rebuildable_bytes, p.git_state, json.dumps(p.git_info)) for p in projects])
    con.execute("COMMIT")
    n_warn = sum(1 for p in projects if p.git_state == "warn")
    n_none = sum(1 for p in projects if p.git_state == "none")
    log(f"  git: {len(projects) - n_warn - n_none} ok, {n_warn} with unpushed work, {n_none} without git; "
        f"{len(updates)} rebuildable folders resolved")
