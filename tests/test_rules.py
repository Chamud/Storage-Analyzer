from storage_analyzer.rules import glob_to_regex, load_rules


def m(pattern, path):
    return bool(glob_to_regex(pattern).match(path))


def test_glob_basics():
    assert m("?:\\Windows", "C:\\Windows")
    assert m("?:\\Windows", "d:\\windows")
    assert not m("?:\\Windows", "C:\\Windows\\Temp")
    assert m("?:\\Windows\\*", "C:\\Windows\\Temp")
    assert not m("?:\\Windows\\*", "C:\\Windows\\Temp\\x")
    assert m("?:\\Windows\\**", "C:\\Windows\\Temp\\x\\y")


def test_glob_expansions():
    assert m("~\\Downloads", "C:\\Users\\chamu\\Downloads")
    assert m("%LOCALAPPDATA%\\Temp", "C:\\Users\\someone\\AppData\\Local\\Temp")
    assert m("%TEMP%", "D:\\Users\\x\\AppData\\Local\\Temp")
    assert m("%PROGRAMDATA%\\Epic", "C:\\ProgramData\\Epic")
    assert not m("~\\Downloads", "C:\\Users\\Downloads")


def test_glob_escapes_regex_chars():
    assert m("?:\\Program Files (x86)", "C:\\Program Files (x86)")
    assert m("?:\\$Recycle.Bin", "C:\\$Recycle.Bin")
    assert not m("?:\\$Recycle.Bin", "C:\\$RecycleXBin")


def test_default_rules_load_and_are_unique():
    rs = load_rules()
    ids = [r.id for r in rs.rules]
    assert len(ids) == len(set(ids)), "duplicate rule ids"
    assert all(r.category in rs.categories for r in rs.rules), "rule with unknown category"
    # a few sanity checks on priorities: specific beats generic
    order = {r.id: i for i, r in enumerate(rs.rules)}
    assert order["pip-cache"] < order["generic-app-cache"]
    assert order["unity-editors"] < order["programfiles-app"]
    assert order["steam-game"] < order["programfiles-app"]


def test_rule_matching_fields():
    rs = load_rules()
    hib = rs.by_id("hiberfil")
    assert hib.matches("C:\\hiberfil.sys", "hiberfil.sys", False, 10, None)
    inst = rs.by_id("downloads-installers")
    assert inst.matches("C:\\Users\\me\\Downloads\\setup.exe", "setup.exe", False, 5_000_000, None)
    assert not inst.matches("C:\\Users\\me\\Desktop\\setup.exe", "setup.exe", False, 5_000_000, None)
    assert not inst.matches("C:\\Users\\me\\Downloads\\tiny.exe", "tiny.exe", False, 10, None)
    nm = rs.by_id("dev-node-modules")
    assert nm.matches("D:\\p\\app\\node_modules", "node_modules", True, 1, ["node"])
    assert not nm.matches("D:\\p\\app\\node_modules", "node_modules", True, 1, None)
    unity = rs.by_id("dev-unity")
    assert unity.matches("D:\\p\\Library", "Library", True, 1, ["unity"])
    assert not unity.matches("D:\\p\\Library", "Library", True, 1, ["python"])
