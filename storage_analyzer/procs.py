"""Which folders are in use by running processes? Used to downgrade "safe" folders to review and to refuse
cleanup of something a process is running from (an MCP server's dist/, a browser's cache, a venv's python).

Only executable paths and command-line arguments are inspected, not open file handles (that would need admin
or a driver). It catches the common case: a program running *from* the folder.
"""

from __future__ import annotations

import json
import re
import subprocess  # noqa: F401 (used below)
from dataclasses import dataclass

_PATH_RE = re.compile(r'[A-Za-z]:\\[^"\'<>|?*\r\n]+?(?=["\'\s]|$)')


@dataclass(frozen=True)
class Proc:
    pid: int
    name: str
    paths: tuple[str, ...]  # lower-cased absolute paths found in exe path + command line


import os

OWN_MARKERS = ("storage_analyzer", "storage-analyzer")  # our own server/CLI/tests mention paths as arguments


def list_processes() -> list[Proc]:
    ps = ("Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, Name, ExecutablePath, CommandLine | "
          "ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rows = json.loads(r.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    if isinstance(rows, dict):
        rows = [rows]
    # Skip ourselves, our ancestors (the shell that launched us) and anything that is part of this tool:
    # their command lines mention folders as *arguments*, which is not "running from" them.
    by_pid = {int(r.get("ProcessId") or 0): r for r in rows}
    skip: set[int] = set()
    pid = os.getpid()
    while pid in by_pid and pid not in skip:
        skip.add(pid)
        pid = int(by_pid[pid].get("ParentProcessId") or 0)
    out = []
    for row in rows:
        cl = (row.get("CommandLine") or "")
        if int(row.get("ProcessId") or 0) in skip or any(m in cl.lower() for m in OWN_MARKERS):
            continue
        if row.get("Name", "").lower() in ("powershell.exe", "pwsh.exe", "cmd.exe") and " -c" in cl.lower():
            continue  # transient inline scripts (e.g. "powershell -Command ...") quoting paths
        paths: set[str] = set()
        if row.get("ExecutablePath"):
            paths.add(row["ExecutablePath"].lower())
        for m in _PATH_RE.findall(row.get("CommandLine") or ""):
            paths.add(m.rstrip("\\").lower())
        if paths:
            out.append(Proc(int(row.get("ProcessId") or 0), row.get("Name") or "?", tuple(sorted(paths))))
    return out


class InUseIndex:
    """Answers "is anything running from under this folder?" for many folders quickly."""

    def __init__(self, procs: list[Proc] | None = None):
        self.procs = procs if procs is not None else list_processes()

    def users_of(self, folder: str) -> list[Proc]:
        f = folder.rstrip("\\").lower()
        prefix = f + "\\"
        return [p for p in self.procs if any(x == f or x.startswith(prefix) for x in p.paths)]

    def describe(self, folder: str) -> str | None:
        users = self.users_of(folder)
        if not users:
            return None
        names = sorted({f"{p.name} (pid {p.pid})" for p in users})
        return "In use right now by " + ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")


