import json

from storage_analyzer import refs as refs_mod
from storage_analyzer.refs import Ref, RefIndex, _mcp_servers, _paths_in


def test_paths_in_handles_json_escaped_backslashes():
    text = json.dumps({"command": "D:\\Tools\\runtime\\node.exe", "args": ["D:\\Tools\\dist\\index.js", "-a", "x"]})
    assert "d:\\tools\\runtime\\node.exe" in _paths_in(text)
    assert "d:\\tools\\dist\\index.js" in _paths_in(text)


def test_mcp_servers_found_at_any_depth():
    doc = {"mcpServers": {"a": {"command": "x"}}, "projects": {"p": {"mcpServers": {"b": {"command": "y"}}}}}
    assert set(_mcp_servers(doc)) == {"a", "b"}


def test_collect_reads_claude_desktop_config(tmp_path, monkeypatch):
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {"lark": {"command": "D:\\P\\desk\\runtime\\node.exe",
                                                       "args": ["D:\\P\\desk\\dist\\index.js"]}}}), encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("PATH", "C:\\Windows;D:\\P\\tools\\bin")
    monkeypatch.setattr(refs_mod, "CONFIG_FILES", [("Claude Desktop config", lambda: cfg)])
    idx = RefIndex()
    assert idx.describe("D:\\P\\desk\\dist") == "Referenced by Claude Desktop config (lark)"
    assert idx.describe("D:\\P\\desk") == "Referenced by Claude Desktop config (lark)"
    assert idx.describe("D:\\P\\tools") == "Referenced by PATH"
    assert idx.describe("D:\\P\\other") is None


def test_describe_lists_sources():
    idx = RefIndex([Ref("Claude Code MCP", "x", "d:\\a\\b"), Ref("Cursor MCP", "y", "d:\\a\\b\\c.js")])
    assert idx.describe("D:\\a") == "Referenced by Claude Code MCP (x), Cursor MCP (y)"
