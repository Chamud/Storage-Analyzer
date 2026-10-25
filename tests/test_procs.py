from storage_analyzer import actions, db
from storage_analyzer.procs import InUseIndex, Proc


def idx():
    return InUseIndex([
        Proc(1, "node.exe", ("c:\\program files\\nodejs\\node.exe", "d:\\projects\\lark\\extend\\dist\\index.js")),
        Proc(2, "chrome.exe", ("c:\\users\\me\\appdata\\local\\google\\chrome\\application\\chrome.exe",)),
    ])


def test_users_of_matches_folder_prefix_only():
    i = idx()
    assert [p.pid for p in i.users_of("D:\\Projects\\lark\\extend\\dist")] == [1]
    assert [p.pid for p in i.users_of("D:\\Projects\\lark\\extend")] == [1]
    assert [p.pid for p in i.users_of("D:\\Projects\\lark")] == [1]
    assert i.users_of("D:\\Projects\\lark\\extend\\dist2") == []
    assert i.users_of("D:\\Projects\\lark\\extend\\node_modules") == []
    assert i.describe("D:\\Projects\\lark\\extend\\dist").startswith("In use right now by node.exe (pid 1)")
    assert i.describe("C:\\Temp") is None


def test_plan_refuses_folders_in_use(tmp_path):
    con = db.connect(tmp_path / "t.db")
    con.execute("INSERT INTO scans(started_at,scanner,status) VALUES('x','test','done')")
    sid = con.execute("SELECT max(id) FROM scans").fetchone()[0]
    d = tmp_path / "dist"
    d.mkdir()
    con.execute("INSERT INTO nodes(id,scan_id,parent_id,drive,path,name,is_dir,depth,size,allocated) VALUES(1,?,NULL,'D',?,?,1,3,1,1)",
                (sid, str(d), "dist"))
    con.execute("INSERT INTO classifications(scan_id,node_id,category,safety,source,created_at) VALUES(?,1,'dev','safe','rule','x')", (sid,))
    busy = InUseIndex([Proc(9, "node.exe", (str(d).lower() + "\\index.js",))])
    a = actions.plan(con, sid, [1], in_use=busy)[0]
    assert a.refused and "node.exe (pid 9)" in a.refused
    assert actions.plan(con, sid, [1], in_use=InUseIndex([]))[0].refused is None
