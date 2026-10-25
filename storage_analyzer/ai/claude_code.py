"""Ask Claude Code (`claude -p`) to classify folders the rules could not name. Metadata only, no tools.

Contract (docs/AI_HELPER.md): the model returns, per folder, {category, safety, confidence, reason, action,
suggested_rule}. `safety` is capped: "safe" is downgraded to "review". Answers are cached in ai_cache.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime

from .. import config
from ..rules import load_rules

MODEL = os.environ.get("SA_AI_MODEL", "sonnet")
BATCH = 20
MAX_WALK_FILES = 3000
MAX_CHILDREN = 25

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "category": {"type": "string"},
                    "safety": {"type": "string", "enum": ["safe", "review", "keep"]},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                    "action": {"type": "string"},
                    "method": {"type": "string", "enum": ["recycle", "command", "manual", "none"]},
                    "suggested_rule": {"type": ["object", "null"]},
                },
                "required": ["path", "category", "safety", "confidence", "reason", "action", "method"],
            },
        }
    },
    "required": ["items"],
}


def is_available() -> bool:
    return shutil.which("claude") is not None


# ----------------------------------------------------------------------------------------------------------
# metadata

_installed: list[str] | None = None


def installed_programs() -> list[str]:
    global _installed
    if _installed is not None:
        return _installed
    names: set[str] = set()
    try:
        import winreg
        for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                          (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for i in range(winreg.QueryInfoKey(k)[0]):
                        try:
                            with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sk:
                                names.add(str(winreg.QueryValueEx(sk, "DisplayName")[0]))
                        except OSError:
                            continue
            except OSError:
                continue
    except ImportError:
        pass
    _installed = sorted(names)
    return _installed


def _installed_match(name: str) -> str | None:
    n = name.lower()
    toks = [t for t in n.replace("-", " ").replace("_", " ").split() if len(t) > 2]
    for prog in installed_programs():
        pl = prog.lower()
        if n in pl or any(t in pl for t in toks):
            return prog
    return None


def summarize(con: sqlite3.Connection, scan_id: int, node: sqlite3.Row) -> dict:
    """Metadata for one folder/file: sizes, counts, dates, extension mix, biggest children. No contents."""
    path = node["path"]
    d: dict = {"path": path, "is_dir": bool(node["is_dir"]), "allocated_gb": round(node["allocated"] / 1e9, 2),
               "files": node["files"], "folders": node["folders"], "newest": (node["mtime"] or "")[:10]}
    if node["is_dir"]:
        kids = con.execute("SELECT name, allocated, is_dir FROM nodes WHERE scan_id=? AND parent_id=? "
                           "ORDER BY allocated DESC LIMIT ?", (scan_id, node["id"], MAX_CHILDREN)).fetchall()
        d["children"] = [[k["name"] + ("\\" if k["is_dir"] else ""), int(k["allocated"])] for k in kids]
        # parts of this folder a rule already handled: the model should judge the remainder
        handled = con.execute(
            "SELECT n.path, c.safety, c.excl_bytes FROM classifications c JOIN nodes n ON n.id=c.node_id "
            "WHERE c.scan_id=? AND n.path>? AND n.path<? AND c.safety!='unknown' ORDER BY c.excl_bytes DESC LIMIT 8",
            (scan_id, path + "\\", path + "]")).fetchall()
        if handled:
            d["already_classified_inside"] = [[h["path"][len(path) + 1:], h["safety"], int(h["excl_bytes"])] for h in handled]
            d["unclassified_gb"] = round((node["allocated"] - sum(h["excl_bytes"] for h in handled)) / 1e9, 2)
        exts: Counter = Counter()
        oldest = None
        seen = 0
        markers = []
        for root, dirs, files in os.walk(path):
            if seen == 0:
                markers = [f for f in files if f.lower() in ("package.json", "pyproject.toml", "requirements.txt",
                                                            "readme.md", "readme.txt", ".gitignore", "cargo.toml")]
            for f in files:
                seen += 1
                ext = os.path.splitext(f)[1].lower() or "(none)"
                exts[ext] += 1
                if seen % 200 == 0:
                    try:
                        m = os.path.getmtime(os.path.join(root, f))
                        oldest = m if oldest is None else min(oldest, m)
                    except OSError:
                        pass
                if seen >= MAX_WALK_FILES:
                    break
            if seen >= MAX_WALK_FILES:
                break
        total = sum(exts.values()) or 1
        d["ext_mix"] = {e: round(c / total, 2) for e, c in exts.most_common(8)}
        d["sampled_files"] = seen
        if oldest:
            d["oldest"] = datetime.fromtimestamp(oldest).date().isoformat()
        d["markers"] = markers
    d["installed_match"] = _installed_match(node["name"])
    return d


def fingerprint(node: sqlite3.Row) -> str:
    return f"{node['allocated']}|{node['mtime']}|{node['files']}"


# ----------------------------------------------------------------------------------------------------------
# calling Claude Code

def _prompt(rs_categories: dict, items: list[dict]) -> str:
    cats = "\n".join(f"- {k}: {v.get('name')} — {v.get('desc')}" for k, v in rs_categories.items() if k != "unknown")
    return f"""You are helping a Windows user free disk space. For each folder/file below, decide what it is and whether it is safe to delete. You only see metadata (sizes, counts, dates, extension mix, largest children, whether a matching program is installed). Never assume file contents.

Categories (use the key):
{cats}

Safety:
- "safe": re-downloadable, rebuilt automatically, or pure cache/temp. Be strict: when in doubt say "review".
- "review": probably removable but the user must look (installers, exports, old client data, app data mixing cache and settings).
- "keep": user's own work, settings, licenses, anything not recoverable.

For each item give: category, safety, confidence (0-1), a one-sentence reason a non-expert understands, a one-sentence action (what to do and how: delete to Recycle Bin, clear inside app X, uninstall via Y), method (recycle | command | manual | none), and optionally suggested_rule: a rule in this YAML-as-JSON shape that would classify this and similar folders next time, or null:
{{"id": "corsair-icue-data", "match": {{"path": "%PROGRAMDATA%\\\\Corsair"}}, "category": "apps", "safety": "keep", "reason": "...", "action": "..."}}
Path globs: `*` one segment, `**` any depth, `?:` any drive, `~` any user profile, %LOCALAPPDATA% %APPDATA% %TEMP% %PROGRAMDATA%.

Return the paths exactly as given. Items:
{json.dumps(items, ensure_ascii=False, indent=1)}"""


def ask(items: list[dict], model: str = MODEL, timeout: int = 300) -> list[dict]:
    rs = load_rules()
    cmd = ["claude", "-p", "--output-format", "json", "--model", model, "--tools", "",
           "--json-schema", json.dumps(SCHEMA), "--max-turns", "1"]
    r = subprocess.run(cmd, input=_prompt(rs.categories, items), capture_output=True, text=True,
                       encoding="utf-8", timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError(f"claude exited {r.returncode}: {r.stderr.strip()[:500] or r.stdout.strip()[:500]}")
    env = json.loads(r.stdout)
    data = env.get("structured_output")
    if data is None:
        text = env.get("result", "")
        if "```" in text:
            text = text.split("```")[1].removeprefix("json")
        data = json.loads(text)
    if isinstance(data, list):
        data = {"items": data}
    out = []
    for it in data["items"]:
        it = dict(it)
        if it.get("safety") == "safe":
            it["safety"] = "review"
            it["reason"] = (it.get("reason") or "") + " (AI suggested safe; shown as review because only rules may auto-delete.)"
        if it.get("category") not in rs.categories:
            it["category"] = "unknown"
        it["confidence"] = max(0.0, min(1.0, float(it.get("confidence") or 0)))
        it["model"] = model
        out.append(it)
    return out


# ----------------------------------------------------------------------------------------------------------
# orchestration

def pending(con: sqlite3.Connection, scan_id: int, min_bytes: int = 100 * 1024 * 1024, drive: str | None = None,
            node_ids: list[int] | None = None) -> list[sqlite3.Row]:
    q = ("SELECT n.* FROM classifications c JOIN nodes n ON n.id=c.node_id WHERE c.scan_id=? AND c.safety='unknown' "
         "AND c.excl_bytes>=? ")
    args: list = [scan_id, min_bytes]
    if drive:
        q += "AND n.drive=? "
        args.append(drive)
    if node_ids:
        q += "AND n.id IN (%s) " % ",".join("?" * len(node_ids))
        args += node_ids
    q += "ORDER BY c.excl_bytes DESC"
    return con.execute(q, args).fetchall()


def cached(con: sqlite3.Connection, node: sqlite3.Row) -> dict | None:
    row = con.execute("SELECT response FROM ai_cache WHERE path=? AND fingerprint=?",
                      (node["path"].lower(), fingerprint(node))).fetchone()
    return json.loads(row["response"]) if row else None


def apply(con: sqlite3.Connection, scan_id: int, node: sqlite3.Row, ans: dict) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    con.execute("INSERT INTO ai_cache(path,fingerprint,model,response,created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(path,fingerprint) DO UPDATE SET response=excluded.response, model=excluded.model, "
                "created_at=excluded.created_at",
                (node["path"].lower(), fingerprint(node), ans.get("model"), json.dumps(ans), now))
    con.execute("UPDATE classifications SET category=?, safety=?, source='ai', rule_id=NULL, confidence=?, reason=?, "
                "action=?, method=?, created_at=? WHERE scan_id=? AND node_id=?",
                (ans["category"], ans["safety"], ans["confidence"], ans["reason"], ans["action"],
                 ans.get("method") or "recycle", now, scan_id, node["id"]))


def run(con: sqlite3.Connection, scan_id: int, *, model: str = MODEL, dry_run: bool = False, limit: int | None = None,
        drive: str | None = None, node_ids: list[int] | None = None, cache_only: bool = False, log=print,
        progress=None) -> dict:
    nodes = pending(con, scan_id, min_bytes=0 if cache_only else 100 * 1024 * 1024, drive=drive, node_ids=node_ids)
    if limit:
        nodes = nodes[:limit]
    hits = 0
    todo: list[sqlite3.Row] = []
    for n in nodes:
        c = cached(con, n)
        if c:
            apply(con, scan_id, n, c)
            hits += 1
        else:
            todo.append(n)
    log(f"{len(nodes)} unknown items: {hits} answered from cache, {len(todo)} to ask")
    if not todo or cache_only:
        return {"cached": hits, "asked": 0}
    if not is_available() and not dry_run:
        raise RuntimeError("Claude Code CLI (`claude`) not found on PATH. Install it or run with --dry-run.")
    asked = 0
    n_batches = (len(todo) + BATCH - 1) // BATCH
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        b = i // BATCH + 1
        if progress:
            progress(i, len(todo))
        log(f"  batch {b}/{n_batches}: summarising {len(batch)} items...")
        items = [summarize(con, scan_id, n) for n in batch]
        if dry_run:
            log(json.dumps(items, indent=1, ensure_ascii=False))
            continue
        import time
        t0 = time.time()
        log(f"  batch {b}/{n_batches}: asking {model} (usually 1-3 minutes per batch, 5 minute limit)...")
        answers = {a["path"].lower(): a for a in ask(items, model=model)}
        log(f"  batch {b}/{n_batches}: answered in {time.time() - t0:.0f}s")
        for n in batch:
            a = answers.get(n["path"].lower())
            if not a:
                log(f"  no answer for {n['path']}")
                continue
            apply(con, scan_id, n, a)
            asked += 1
            log(f"  {a['safety']:6} {a['category']:9} {a['confidence']:.0%}  {n['path']}  — {a['reason']}")
        if progress:
            progress(min(i + BATCH, len(todo)), len(todo))
    return {"cached": hits, "asked": asked}
