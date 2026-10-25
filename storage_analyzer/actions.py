"""Cleanup actions: Recycle Bin deletes, whitelisted native commands, and "open the right tool" for manual ones.

Safety invariants (also in CLAUDE.md):
  * refuse anything under a `keep` root (unless a closer classified root says otherwise), drive roots, and
    depth-1 system folders
  * default is the Recycle Bin via send2trash; permanent deletion is not offered here
  * only commands that appear in the loaded rules may run (no free-form shell)
  * everything is written to cleanup_log, including dry runs and refusals
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config
from .rules import load_rules

PROTECTED_DEPTH1 = {"windows", "program files", "program files (x86)", "programdata", "users", "$recycle.bin",
                    "system volume information", "recovery", "boot", "efi"}


@dataclass
class Action:
    node_id: int
    path: str
    bytes: int
    method: str                 # recycle | command | manual | none
    command: str | None = None
    admin: bool = False
    safety: str = "unknown"
    reason: str = ""
    refused: str | None = None  # why this will not run
    result: str | None = None
    detail: str | None = None


def _classification_chain(con: sqlite3.Connection, scan_id: int, path: str) -> list[sqlite3.Row]:
    """Classifications of `path` and all its ancestors, nearest first."""
    chain = []
    cut = path
    while True:
        row = con.execute("SELECT c.* FROM classifications c JOIN nodes n ON n.id=c.node_id "
                          "WHERE c.scan_id=? AND n.path=?", (scan_id, cut)).fetchone()
        if row:
            chain.append(row)
        if len(cut) <= 3 or "\\" not in cut:
            break
        cut = cut[:cut.rfind("\\")] or cut[:3]
        if len(cut) == 2:
            cut += "\\"
    return chain


def is_protected(con: sqlite3.Connection, scan_id: int, path: str) -> str | None:
    """Returns a reason string if the path must not be touched, else None."""
    p = path.rstrip("\\")
    if len(p) <= 2:
        return "drive root"
    parts = p.split("\\")
    chain = _classification_chain(con, scan_id, p)
    own = chain[0] if chain and con.execute("SELECT path FROM nodes WHERE id=?", (chain[0]["node_id"],)).fetchone()["path"].lower() == p.lower() else None
    # Top-level system folders are off limits unless a rule explicitly handled that exact folder
    # (e.g. C:\$Recycle.Bin → "Empty the Recycle Bin" command).
    if len(parts) == 2 and parts[1].lower() in PROTECTED_DEPTH1 and not (own and own["safety"] in ("safe", "review")):
        return "top-level system folder"
    if not chain:
        # Nothing at or above this path was classified: a container like C:\Users\chamu, or a small
        # folder nobody looked at. The app only cleans what it has classified; use Explorer for the rest.
        return "not classified: look inside and pick specific items"
    nearest = chain[0]
    if nearest["safety"] == "keep":
        return f"marked keep ({nearest['rule_id'] or nearest['source']}): {nearest['reason']}"
    return None


def plan(con: sqlite3.Connection, scan_id: int, node_ids: list[int], in_use=None) -> list[Action]:
    from .procs import InUseIndex
    rs = load_rules()
    allowed_cmds = {r.command for r in rs.rules if r.command}
    in_use = in_use if in_use is not None else InUseIndex()
    out: list[Action] = []
    for nid in node_ids:
        n = con.execute("SELECT * FROM nodes WHERE id=? AND scan_id=?", (nid, scan_id)).fetchone()
        if not n:
            out.append(Action(nid, "?", 0, "none", refused="node not found in this scan"))
            continue
        c = con.execute("SELECT * FROM classifications WHERE scan_id=? AND node_id=?", (scan_id, nid)).fetchone()
        method = c["method"] if c else "recycle"
        cmd = c["command"] if c else None
        a = Action(nid, n["path"], n["allocated"], method or "recycle", cmd, bool(c and c["admin"]),
                   c["safety"] if c else "unknown", c["reason"] if c else "")
        why = is_protected(con, scan_id, n["path"])
        if why:
            a.refused = why
        elif a.method == "command" and a.command not in allowed_cmds:
            a.refused = f"command not in rules whitelist: {a.command}"
        elif a.method == "none":
            a.refused = "nothing to do for this item"
        elif a.method == "recycle" and not os.path.exists(n["path"]):
            a.refused = "path no longer exists (rescan)"
        elif a.method == "recycle" and n["is_dir"] and (busy := in_use.describe(n["path"])):
            a.refused = busy + "; close it and try again"
        out.append(a)
    return out


# ----------------------------------------------------------------------------------------------------------

def _log(con: sqlite3.Connection, a: Action) -> None:
    con.execute("INSERT INTO cleanup_log(ts,path,bytes,method,command,result,detail) VALUES(?,?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), a.path, a.bytes, a.method, a.command, a.result,
                 (a.detail or "")[:2000]))


def _recycle_one(path: str) -> str | None:
    """send2trash a path. If a folder fails (typically a locked file inside), fall back to recycling its
    children one by one so everything that *can* go, goes. Returns None on full success, else a message."""
    from send2trash import send2trash
    try:
        send2trash(path)
        return None
    except Exception as ex:  # noqa: BLE001
        if not os.path.isdir(path):
            return str(ex)
    ok = fail = 0
    first_err = ""
    try:
        for name in os.listdir(path):
            child = os.path.join(path, name)
            try:
                send2trash(child)
                ok += 1
            except Exception as ex2:  # noqa: BLE001
                fail += 1
                first_err = first_err or f"{name}: {ex2}"
    except OSError as ex3:
        return str(ex3)
    if fail == 0:
        return None
    return f"partial: {ok} items moved, {fail} in use and skipped (close the app that uses them and retry). First: {first_err}"


def _recycle_many(paths: list[str], progress=None) -> list[tuple[str, str | None]]:
    """Recycle each path; returns [(path, error_or_None)]. `progress(i, n, path, err)` after each one."""
    res = []
    for i, p in enumerate(paths, 1):
        err = _recycle_one(p)
        res.append((p, err))
        if progress:
            progress(i, len(paths), p, err)
    return res


def _recycle_elevated(paths: list[str]) -> list[tuple[str, str | None]]:
    """Run this module elevated to recycle admin-only paths (e.g. C:\\Windows\\LiveKernelReports)."""
    config.ensure_dirs()
    with tempfile.NamedTemporaryFile("w", suffix=".json", dir=config.LOG_DIR, delete=False, encoding="utf-8") as f:
        json.dump(paths, f)
        req = f.name
    resp = req + ".out"
    ps = (f"$p = Start-Process -FilePath '{sys.executable}' -ArgumentList '-m','storage_analyzer.actions','--recycle-file',"
          f"'\"{req}\"','--out','\"{resp}\"' -Verb RunAs -PassThru -Wait -WindowStyle Hidden; exit $p.ExitCode")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(resp):
        return [(p, "admin prompt declined or elevated run failed") for p in paths]
    out = json.loads(Path(resp).read_text(encoding="utf-8"))
    os.unlink(req)
    os.unlink(resp)
    return [(p, e) for p, e in out]


def _run_command(cmd: str, admin: bool) -> tuple[bool, str]:
    if admin:
        ps = (f"$p = Start-Process powershell -ArgumentList '-NoProfile','-Command','{cmd}' -Verb RunAs -PassThru -Wait "
              "-WindowStyle Hidden; exit $p.ExitCode")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=1800)
        return r.returncode == 0, (r.stdout + r.stderr).strip() or f"exit {r.returncode}"
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=1800)
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-2000:] or f"exit {r.returncode}"


def execute(con: sqlite3.Connection, actions: list[Action], dry_run: bool = True, log=None, progress=None) -> list[Action]:
    """Run the plan. Order matters: commands first (so "Empty the Recycle Bin" only empties what was in the
    bin BEFORE this cleanup and the items we move there afterwards stay recoverable), then user-level
    Recycle Bin moves, then admin-only moves through one elevated helper. Every action is logged as it
    completes, so a crash or a closed browser still leaves a record. `progress(done, total)` after each."""
    log = log or (lambda *a, **k: None)
    total = len(actions)
    count = 0

    def done(a: Action, result: str, detail: str | None):
        nonlocal count
        a.result, a.detail = result, detail
        _log(con, a)
        count += 1
        if progress:
            progress(count, total)
        log(f"  {result:7} {a.path}" + (f"  ({detail})" if detail and result != "ok" else ""))

    for a in actions:
        if a.refused:
            done(a, "skipped", a.refused)
        elif dry_run:
            done(a, "dry-run", {"recycle": "would move to Recycle Bin", "command": f"would run: {a.command}",
                                "manual": "would open the tool / show instructions"}.get(a.method, ""))
    if dry_run:
        return actions

    seen_cmds: set[str] = set()
    cmds = [a for a in actions if not a.refused and a.method == "command"]
    if cmds:
        log(f"Running {len(cmds)} cleanup command(s)...")
    for a in cmds:
        if a.command in seen_cmds:  # e.g. both Recycle Bins share one command
            done(a, "ok", "covered by an earlier identical command")
            continue
        seen_cmds.add(a.command)
        log(f"  running: {a.command}" + (" (admin)" if a.admin else ""))
        ok, out = _run_command(a.command, a.admin)
        done(a, "ok" if ok else "failed", out)

    recycle_user = [a for a in actions if not a.refused and a.method == "recycle" and not a.admin]
    if recycle_user:
        log(f"Moving {len(recycle_user)} item(s) to the Recycle Bin...")
        by_path = {a.path: a for a in recycle_user}
        _recycle_many([a.path for a in recycle_user],
                      progress=lambda i, n, p, err: done(by_path[p], "failed" if err else "ok", err or "moved to Recycle Bin"))

    recycle_admin = [a for a in actions if not a.refused and a.method == "recycle" and a.admin]
    if recycle_admin:
        log(f"Moving {len(recycle_admin)} admin-only item(s) (approve the Windows prompt)...")
        for a, (_, err) in zip(recycle_admin, _recycle_elevated([a.path for a in recycle_admin])):
            done(a, "failed" if err else "ok", err or "moved to Recycle Bin")

    for a in actions:
        if not a.refused and a.method == "manual":
            done(a, "skipped", "manual step: " + (a.command or "see the action text"))
    freed = sum(a.bytes for a in actions if a.result == "ok")
    log(f"Finished: {sum(1 for a in actions if a.result == 'ok')} ok, {sum(1 for a in actions if a.result == 'failed')} failed, "
        f"{sum(1 for a in actions if a.result == 'skipped')} skipped. About {freed / 1e9:.1f} GB freed.")
    return actions


def _elevated_main(argv: list[str]) -> int:
    """Entry used by _recycle_elevated inside the elevated process."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--recycle-file", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    paths = json.loads(Path(a.recycle_file).read_text(encoding="utf-8"))
    res = _recycle_many(paths)
    Path(a.out).write_text(json.dumps(res), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(_elevated_main(sys.argv[1:]))
