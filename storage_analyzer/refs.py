"""Folders referenced by tool configurations: MCP server definitions (Claude Code, Claude Desktop, Cursor,
VS Code, Windsurf), the PATH, and Windows startup entries. A folder can be essential without any process
running from it right now (Claude Desktop closed, a scheduled task idle), so this complements procs.py.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

_PATH_RE = re.compile(r'[A-Za-z]:\\\\?(?:[^"\'<>|?*\r\n\\]+\\\\?)*[^"\'<>|?*\r\n\\]*')


@dataclass(frozen=True)
class Ref:
    source: str      # "Claude Desktop config", "Claude Code MCP", ...
    name: str        # server/entry name when known
    path: str        # lower-cased absolute path


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE", Path.home()))


def _appdata(kind: str = "APPDATA") -> Path:
    return Path(os.environ.get(kind, _home() / "AppData" / ("Roaming" if kind == "APPDATA" else "Local")))


def _paths_in(text: str) -> set[str]:
    out = set()
    for m in _PATH_RE.findall(text or ""):
        p = m.replace("\\\\", "\\").rstrip("\\").strip()
        if len(p) > 3:
            out.add(p.lower())
    return out


def _mcp_servers(doc) -> dict:
    """Collect every mcpServers block anywhere in a JSON document (Claude Code nests them per project)."""
    found: dict = {}
    if isinstance(doc, dict):
        if isinstance(doc.get("mcpServers"), dict):
            found.update(doc["mcpServers"])
        for v in doc.values():
            found.update(_mcp_servers(v))
    elif isinstance(doc, list):
        for v in doc:
            found.update(_mcp_servers(v))
    return found


def _json_file(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


CONFIG_FILES = [
    ("Claude Code MCP", lambda: _home() / ".claude.json"),
    ("Claude Desktop config", lambda: _appdata() / "Claude" / "claude_desktop_config.json"),
    ("Cursor MCP", lambda: _home() / ".cursor" / "mcp.json"),
    ("VS Code MCP", lambda: _appdata() / "Code" / "User" / "mcp.json"),
    ("VS Code settings", lambda: _appdata() / "Code" / "User" / "settings.json"),
    ("Windsurf MCP", lambda: _home() / ".codeium" / "windsurf" / "mcp_config.json"),
]


def collect() -> list[Ref]:
    refs: list[Ref] = []
    for source, fn in CONFIG_FILES:
        doc = _json_file(fn())
        if doc is None:
            continue
        servers = _mcp_servers(doc)
        if servers:
            for name, spec in servers.items():
                for p in _paths_in(json.dumps(spec)):
                    refs.append(Ref(source, str(name), p))
        elif source == "VS Code settings":
            for p in _paths_in(json.dumps(doc)):
                refs.append(Ref(source, "settings", p))
    for entry in os.environ.get("PATH", "").split(";"):
        e = entry.strip().rstrip("\\")
        if len(e) > 3 and ":" in e:
            refs.append(Ref("PATH", "", e.lower()))
    try:
        import winreg
        for hive, hname in ((winreg.HKEY_CURRENT_USER, "HKCU"), (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                    for i in range(winreg.QueryInfoKey(k)[1]):
                        name, val, _ = winreg.EnumValue(k, i)
                        for p in _paths_in(str(val)):
                            refs.append(Ref(f"{hname} startup entry", name, p))
            except OSError:
                continue
    except ImportError:
        pass
    return refs


class RefIndex:
    def __init__(self, refs: list[Ref] | None = None):
        self.refs = refs if refs is not None else collect()

    def refs_to(self, folder: str) -> list[Ref]:
        f = folder.rstrip("\\").lower()
        prefix = f + "\\"
        return [r for r in self.refs if r.path == f or r.path.startswith(prefix)]

    def describe(self, folder: str) -> str | None:
        rs = self.refs_to(folder)
        if not rs:
            return None
        labels = sorted({f"{r.source}{' (' + r.name + ')' if r.name else ''}" for r in rs})
        return "Referenced by " + ", ".join(labels[:3]) + (f" and {len(labels) - 3} more" if len(labels) > 3 else "")
