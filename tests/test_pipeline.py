"""End-to-end on a synthetic drive: entries → importer → rules → projects (no git, no WizTree)."""

from pathlib import Path

import pytest

from storage_analyzer import actions, db
from storage_analyzer.rules import classify, load_rules
from storage_analyzer.scanner import Entry
from storage_analyzer.scanner.importer import import_entries

GB = 1024 ** 3


def tree():
    def d(path, size, files=10, folders=1, mtime="2026-01-01T00:00:00"):
        return Entry(path, True, size, size, mtime, 16, files, folders)

    def f(path, size, mtime="2026-01-01T00:00:00"):
        return Entry(path, False, size, size, mtime, 32, 0, 0)

    return [
        d("C:\\", 100 * GB),
        f("C:\\hiberfil.sys", 12 * GB),
        d("C:\\Windows", 40 * GB),
        d("C:\\Windows\\System32", 20 * GB),
        d("C:\\Windows\\LiveKernelReports", 8 * GB),
        d("C:\\Users", 40 * GB),
        d("C:\\Users\\me", 40 * GB),
        d("C:\\Users\\me\\AppData", 30 * GB),
        d("C:\\Users\\me\\AppData\\Local", 30 * GB),
        d("C:\\Users\\me\\AppData\\Local\\pip", 4 * GB),
        d("C:\\Users\\me\\AppData\\Local\\pip\\cache", 4 * GB),
        d("C:\\Users\\me\\AppData\\Local\\SomeApp", 3 * GB),
        d("C:\\Users\\me\\AppData\\Local\\SomeApp\\Cache", 2 * GB),
        d("C:\\Users\\me\\Downloads", 5 * GB),
        f("C:\\Users\\me\\Downloads\\big-installer.exe", 2 * GB),
        d("C:\\Users\\me\\Documents", 3 * GB),
        d("C:\\Users\\me\\src", 2 * GB),
        d("C:\\Users\\me\\src\\app", 2 * GB),
        d("C:\\Users\\me\\src\\app\\node_modules", 1 * GB, mtime="2025-01-01T00:00:00"),
        d("C:\\Users\\me\\src\\app\\.git", 10 * 1024 * 1024),
    ]


@pytest.fixture
def con(tmp_path: Path):
    c = db.connect(tmp_path / "t.db")
    c.execute("INSERT INTO scans(started_at,scanner,status) VALUES('x','test','done')")
    sid = c.execute("SELECT max(id) FROM scans").fetchone()[0]
    import_entries(c, sid, "C", tree(), min_file_bytes=1024 * 1024)
    return c, sid


def classified(con, sid):
    return {r["path"]: dict(r) for r in con.execute(
        "SELECT n.path, c.* FROM classifications c JOIN nodes n ON n.id=c.node_id WHERE c.scan_id=?", (sid,))}


def test_import_links_parents(con):
    c, sid = con
    row = c.execute("SELECT n.name, p.path parent FROM nodes n JOIN nodes p ON p.id=n.parent_id WHERE n.path=?",
                    ("C:\\Users\\me\\AppData\\Local\\pip\\cache",)).fetchone()
    assert row["parent"] == "C:\\Users\\me\\AppData\\Local\\pip"
    assert c.execute("SELECT COUNT(*) FROM nodes WHERE parent_id IS NULL").fetchone()[0] == 1


def test_classification_semantics(con):
    c, sid = con
    rs = load_rules()
    projects = {"c:\\users\\me\\src\\app": ["node"]}
    classify(c, sid, rs, projects, log=lambda *a, **k: None)
    cl = classified(c, sid)
    assert cl["C:\\hiberfil.sys"]["safety"] == "safe"
    assert cl["C:\\Windows"]["safety"] == "keep"
    assert "C:\\Windows\\System32" not in cl, "inside keep, no override rule → inherits"
    assert cl["C:\\Windows\\LiveKernelReports"]["safety"] == "safe", "override rule applies inside keep"
    assert cl["C:\\Windows"]["excl_bytes"] == 40 * GB - 8 * GB, "nested root subtracted"
    assert cl["C:\\Users\\me\\AppData\\Local\\pip"]["rule_id"] == "pip-cache"
    assert "C:\\Users\\me\\AppData\\Local\\pip\\cache" not in cl, "inside a safe root nothing is re-classified"
    assert cl["C:\\Users\\me\\AppData\\Local\\SomeApp"]["safety"] == "unknown", "unmatched child of a container"
    assert cl["C:\\Users\\me\\AppData\\Local\\SomeApp\\Cache"]["rule_id"] == "generic-app-cache", "rules still apply under unknown"
    assert cl["C:\\Users\\me\\AppData\\Local\\SomeApp"]["excl_bytes"] == 1 * GB
    assert cl["C:\\Users\\me\\Downloads\\big-installer.exe"]["safety"] == "review"
    assert cl["C:\\Users\\me\\Documents"]["safety"] == "keep"
    assert cl["C:\\Users\\me\\src\\app"]["rule_id"] == "project-root"
    assert cl["C:\\Users\\me\\src\\app\\node_modules"]["safety"] == "rebuildable"
    assert cl["C:\\Users\\me\\src\\app\\.git"]["safety"] == "keep"
    assert "C:\\Users\\me\\src" not in cl, "folder containing a project is a container, not unknown"


def test_protection(con):
    c, sid = con
    classify(c, sid, load_rules(), {"c:\\users\\me\\src\\app": ["node"]}, log=lambda *a, **k: None)
    assert actions.is_protected(c, sid, "C:\\") == "drive root"
    assert actions.is_protected(c, sid, "C:\\Windows")
    assert actions.is_protected(c, sid, "C:\\Windows\\System32")
    assert actions.is_protected(c, sid, "C:\\Users\\me\\Documents")
    assert actions.is_protected(c, sid, "C:\\Users\\me\\src\\app")
    assert actions.is_protected(c, sid, "C:\\Users\\me"), "unclassified container must be refused"
    assert actions.is_protected(c, sid, "C:\\Users\\me\\AppData\\Local"), "unclassified container must be refused"
    assert actions.is_protected(c, sid, "C:\\Users\\me\\AppData\\Local\\SomeApp") is None, "unknown root is the user's call"
    assert actions.is_protected(c, sid, "C:\\Windows\\LiveKernelReports") is None
    assert actions.is_protected(c, sid, "C:\\Users\\me\\AppData\\Local\\pip") is None
    assert actions.is_protected(c, sid, "C:\\Users\\me\\src\\app\\node_modules") is None


def test_plan_refuses_and_dry_run_logs(con):
    c, sid = con
    classify(c, sid, load_rules(), {}, log=lambda *a, **k: None)
    ids = {r["path"]: r["id"] for r in c.execute("SELECT id, path FROM nodes")}
    acts = actions.plan(c, sid, [ids["C:\\Windows"], ids["C:\\hiberfil.sys"], ids["C:\\Users\\me\\AppData\\Local\\pip"]])
    by = {a.path: a for a in acts}
    assert by["C:\\Windows"].refused
    assert by["C:\\hiberfil.sys"].method == "command" and by["C:\\hiberfil.sys"].admin and not by["C:\\hiberfil.sys"].refused
    assert by["C:\\Users\\me\\AppData\\Local\\pip"].method == "command"
    res = actions.execute(c, acts, dry_run=True)
    assert {a.result for a in res} == {"skipped", "dry-run"}
    assert c.execute("SELECT COUNT(*) FROM cleanup_log").fetchone()[0] == 3
