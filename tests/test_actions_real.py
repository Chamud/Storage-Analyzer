"""Really moves a tiny temp folder to the Recycle Bin, with one file held open to prove the partial fallback.
Leaves two tiny files in the Recycle Bin; harmless."""

import os
from pathlib import Path

import pytest

from storage_analyzer import actions, db


@pytest.mark.skipif(os.name != "nt", reason="Recycle Bin is Windows-only")
def test_execute_recycles_and_reports_locked_files(tmp_path: Path):
    con = db.connect(tmp_path / "log.db")
    target = tmp_path / "sa-test-folder"
    target.mkdir()
    (target / "free.txt").write_text("x")
    locked = target / "locked.txt"
    locked.write_text("y")
    progress = []
    with open(locked, "r+") as fh:  # keeps locked.txt open: send2trash of the folder must fail, children fallback kicks in
        acts = [actions.Action(1, str(target), 2, "recycle", safety="safe")]
        res = actions.execute(con, acts, dry_run=False, log=lambda *a, **k: None, progress=lambda d, t: progress.append((d, t)))
        fh.write("z")
    a = res[0]
    assert a.result == "failed" and a.detail.startswith("partial:"), a.detail
    assert "1 items moved" in a.detail and "1 in use" in a.detail
    assert not (target / "free.txt").exists(), "the free file should have gone to the Recycle Bin"
    assert locked.exists(), "the locked file must still be there"
    assert progress == [(1, 1)]
    rows = con.execute("SELECT result, detail FROM cleanup_log").fetchall()
    assert len(rows) == 1 and rows[0]["result"] == "failed"
    # now that nothing is open, a second attempt succeeds completely
    res2 = actions.execute(con, [actions.Action(1, str(target), 1, "recycle", safety="safe")], dry_run=False)
    assert res2[0].result == "ok" and not target.exists()


def test_commands_run_before_recycle_moves(tmp_path: Path, monkeypatch):
    """Order guarantees "Empty the Recycle Bin" cannot delete what this same cleanup just moved there."""
    con = db.connect(tmp_path / "log.db")
    order = []
    monkeypatch.setattr(actions, "_run_command", lambda cmd, admin: (order.append(("cmd", cmd)) or (True, "ok")))
    monkeypatch.setattr(actions, "_recycle_one", lambda p: order.append(("recycle", p)))
    monkeypatch.setattr(actions, "_recycle_elevated", lambda paths: [(p, order.append(("admin", p))) for p in paths])
    acts = [actions.Action(1, "C:\\x\\a", 1, "recycle"), actions.Action(2, "C:\\$Recycle.Bin", 1, "command", "Clear-RecycleBin -Force"),
            actions.Action(3, "C:\\Windows\\LiveKernelReports", 1, "recycle", admin=True)]
    actions.execute(con, acts, dry_run=False)
    assert [o[0] for o in order] == ["cmd", "recycle", "admin"]
