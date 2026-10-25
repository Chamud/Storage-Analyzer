"""Rule engine: load YAML rules, match them against scanned nodes, write classifications.

Semantics (see docs/RULES.md):
  * Nodes are visited in pre-order. The first matching rule (highest priority, then file order) wins.
  * safety values: safe | review | keep | container | rebuildable
      - safe / review : the node is a "root"; its subtree is not classified further
      - keep          : hard root; descendants are only re-matched by rules with `override: true`
      - container     : "look inside": not reported itself; children that match nothing become `unknown`
                        roots when they are large enough (UNKNOWN_MIN_BYTES)
      - rebuildable   : only inside detected projects; resolved later to safe (stale project) or review
  * Each root records `excl_bytes` = its allocated bytes minus nested roots, so totals never double count.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from . import config

UNKNOWN_MIN_BYTES = int(os.environ.get("SA_UNKNOWN_MIN_BYTES", 100 * 1024 * 1024))

SAFETIES = {"safe", "review", "keep", "container", "rebuildable"}

_EXPANSIONS = {
    "%TEMP%": "~\\AppData\\Local\\Temp",
    "%LOCALAPPDATA%": "~\\AppData\\Local",
    "%APPDATA%": "~\\AppData\\Roaming",
    "%PROGRAMDATA%": "?:\\ProgramData",
    "~": "?:\\Users\\*",
}


def glob_to_regex(pattern: str) -> re.Pattern:
    """Windows path glob → anchored, case-insensitive regex.  `*` = within one segment, `**` = any depth,
    `?` = one char, `~` = any user profile, %TEMP%/%LOCALAPPDATA%/%APPDATA%/%PROGRAMDATA% as usual."""
    for k, v in _EXPANSIONS.items():
        pattern = pattern.replace(k, v)
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append(r"[^\\]*")
        elif c == "?":
            out.append(".")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


@dataclass
class Rule:
    id: str
    category: str
    safety: str
    reason: str = ""
    action: str = ""
    method: str = "recycle"        # recycle | command | manual | none
    command: str | None = None
    admin: bool = False
    override: bool = False
    priority: int = 0
    in_project: bool = False       # only match nodes inside a detected project
    project_types: set[str] = field(default_factory=set)  # ...of one of these types (empty = any)
    is_dir: bool | None = None
    min_bytes: int = 0
    path_res: list[re.Pattern] = field(default_factory=list)
    name_res: list[re.Pattern] = field(default_factory=list)
    exts: set[str] = field(default_factory=set)
    under_res: list[re.Pattern] = field(default_factory=list)
    source_file: str = ""

    def matches(self, path: str, name: str, is_dir: bool, allocated: int, proj_types: list[str] | None) -> bool:
        """proj_types: the enclosing project's types, or None when not inside a project."""
        if self.is_dir is not None and self.is_dir != is_dir:
            return False
        if allocated < self.min_bytes:
            return False
        if self.in_project:
            if proj_types is None:
                return False
            if self.project_types and not (self.project_types & set(proj_types)):
                return False
        if self.path_res and not any(r.match(path) for r in self.path_res):
            return False
        if self.name_res and not any(r.match(name) for r in self.name_res):
            return False
        if self.exts:
            ext = os.path.splitext(name)[1].lower()
            if ext not in self.exts:
                return False
        if self.under_res:
            parent = path[:path.rfind("\\")] if "\\" in path else ""
            if not any(r.match(parent) or r.match(path) for r in self.under_res):
                return False
        return True


@dataclass
class RuleSet:
    categories: dict[str, dict]
    rules: list[Rule]
    project_roots: list[str]
    project_markers: dict

    def by_id(self, rid: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rid), None)


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _parse_rule(d: dict, src: str) -> Rule:
    m = d.get("match") or {}
    safety = d["safety"]
    if safety not in SAFETIES:
        raise ValueError(f"rule {d.get('id')}: bad safety {safety!r}")
    under = [glob_to_regex(p) for p in _as_list(m.get("under"))]
    # `under` must also match any deeper ancestor, so allow a trailing `\**`
    under += [glob_to_regex(p.rstrip("\\") + "\\**") for p in _as_list(m.get("under"))]
    return Rule(
        id=d["id"], category=d.get("category", "unknown"), safety=safety,
        reason=d.get("reason", ""), action=d.get("action", ""), method=d.get("method", "recycle"),
        command=d.get("command"), admin=bool(d.get("admin", False)), override=bool(d.get("override", False)),
        priority=int(d.get("priority", 0)), in_project=bool(m.get("in_project", False)),
        project_types=set(_as_list(m.get("project_types"))),
        is_dir=m.get("is_dir"), min_bytes=int(m.get("min_bytes", 0)),
        path_res=[glob_to_regex(p) for p in _as_list(m.get("path"))],
        name_res=[glob_to_regex(p) for p in _as_list(m.get("name"))],
        exts={e.lower() if e.startswith(".") else "." + e.lower() for e in _as_list(m.get("ext"))},
        under_res=under, source_file=src,
    )


def load_rules(paths: list[Path] | None = None) -> RuleSet:
    paths = paths or [p for p in (config.DEFAULT_RULES, config.USER_RULES) if p.exists()]
    categories: dict[str, dict] = {}
    rules: list[Rule] = []
    project_roots: list[str] = []
    markers: dict = {}
    for i, p in enumerate(paths):
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        categories.update(doc.get("categories") or {})
        project_roots += doc.get("project_roots") or []
        markers.update(doc.get("project_markers") or {})
        for d in doc.get("rules") or []:
            r = _parse_rule(d, p.name)
            r.priority += i * 1000  # later files (user.yaml) beat earlier ones
            rules.append(r)
    # stable sort: higher priority first, then file order
    rules = [r for _, _, r in sorted(((-r.priority, n, r) for n, r in enumerate(rules)), key=lambda t: (t[0], t[1]))]
    return RuleSet(categories, rules, project_roots, markers)


# ----------------------------------------------------------------------------------------------------------
# Classification pass
# ----------------------------------------------------------------------------------------------------------

@dataclass(slots=True)
class Ctx:
    kind: str            # keep | classified | container | unknown | skip
    root_id: int | None  # nearest enclosing classified root (for excl_bytes)


@dataclass(slots=True)
class RootRow:
    node_id: int
    category: str
    safety: str
    source: str
    rule_id: str | None
    reason: str
    action: str
    method: str
    command: str | None
    admin: bool
    excl: int


def classify(con: sqlite3.Connection, scan_id: int, rs: RuleSet, project_types: dict[str, list[str]],
             log=print) -> dict:
    """Classify every node of a scan. `project_types` = {lower-cased project root path: [types]}."""
    project_paths = set(project_types)
    proj_prefixes = sorted(project_paths, key=len)
    # Folders that contain a project somewhere below are "group" folders: look inside instead of reporting
    # them as one big Unknown.
    project_ancestors: set[str] = set()
    for p in project_paths:
        cut = p
        while "\\" in cut:
            cut = cut[:cut.rfind("\\")]
            project_ancestors.add(cut)

    def in_project(lp: str) -> list[str] | None:
        for p in proj_prefixes:
            if lp == p or lp.startswith(p + "\\"):
                return project_types[p]
        return None

    ctx: dict[int, Ctx] = {}
    roots: dict[int, RootRow] = {}
    n = 0
    cur = con.execute("SELECT id,parent_id,path,name,is_dir,allocated FROM nodes WHERE scan_id=? ORDER BY id", (scan_id,))
    for nid, pid, path, name, is_dir, allocated in cur:
        n += 1
        if n % 100000 == 0:
            log(f"  classified {n:,} nodes", end="\r")
        parent = ctx.get(pid) if pid is not None else Ctx("container", None)
        if parent is None:
            parent = Ctx("container", None)

        if parent.kind == "classified":
            ctx[nid] = parent
            continue

        lp = path.lower()
        proj = in_project(lp)
        matched: Rule | None = None
        is_project_root = bool(is_dir) and lp in project_paths and parent.kind != "keep"
        if is_project_root:
            row = RootRow(nid, "projects", "keep", "rule", "project-root",
                          "Your source code. Rebuildable folders inside are listed separately.",
                          "Leave as is (commit and push if git shows a warning)", "none", None, False, allocated)
        else:
            for r in rs.rules:
                if parent.kind == "keep" and not r.override:
                    continue
                if r.matches(path, name, bool(is_dir), allocated, proj):
                    matched = r
                    break
            row = None
            if matched:
                row = RootRow(nid, matched.category, matched.safety, "rule", matched.id, matched.reason,
                              matched.action, matched.method, matched.command, matched.admin, allocated)

        if row is None:
            # no rule
            if parent.kind == "container" and is_dir and lp in project_ancestors:
                ctx[nid] = Ctx("container", parent.root_id)
            elif parent.kind == "container" and allocated >= UNKNOWN_MIN_BYTES:
                row = RootRow(nid, "unknown", "unknown", "none", None,
                              "No rule matched this folder." if is_dir else "No rule matched this file.",
                              "Ask the AI helper or decide yourself", "recycle", None, False, allocated)
                ctx[nid] = Ctx("unknown", nid)
            elif parent.kind == "container":
                ctx[nid] = Ctx("skip", parent.root_id)
            else:
                ctx[nid] = parent  # keep / unknown / skip inherit
        elif row.safety == "container":
            ctx[nid] = Ctx("container", parent.root_id)
            row = None
        else:
            kind = {"keep": "keep", "safe": "classified", "review": "classified",
                    "rebuildable": "classified"}[row.safety]
            ctx[nid] = Ctx(kind, nid)

        if row is not None:
            roots[nid] = row
            if parent.root_id is not None and parent.root_id in roots:
                roots[parent.root_id].excl -= allocated

    log(f"  classified {n:,} nodes; {len(roots):,} roots")
    now = datetime.now().isoformat(timespec="seconds")
    con.execute("BEGIN")
    con.execute("DELETE FROM classifications WHERE scan_id=?", (scan_id,))
    con.executemany(
        "INSERT INTO classifications(scan_id,node_id,category,safety,source,rule_id,confidence,reason,action,"
        "method,command,admin,excl_bytes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(scan_id, r.node_id, r.category, r.safety, r.source, r.rule_id, None, r.reason, r.action, r.method,
          r.command, int(r.admin), max(r.excl, 0), now) for r in roots.values()])
    con.execute("COMMIT")
    counts: dict[str, int] = {}
    for r in roots.values():
        counts[r.safety] = counts.get(r.safety, 0) + 1
    return counts
