"""Command line entry point.

    storage-analyzer scan [--drives C D] [--scanner auto|wiztree|walker] [--no-admin]
    storage-analyzer top  [--drive C] [-n 30]
    storage-analyzer drives
"""

from __future__ import annotations

import argparse
import sys

from . import config, db
from .scanner import list_drives
from .util import human


def cmd_drives(_: argparse.Namespace) -> int:
    for d in list_drives(fixed_only=False):
        print(f"{d.letter}:  {d.fs:6} {human(d.used):>9} used / {human(d.capacity):>9}  free {human(d.free):>9}  {d.label}")
    return 0


def cmd_scan(a: argparse.Namespace) -> int:
    from .scan import run_scan
    config.ensure_dirs()
    con = db.connect()
    scan_id = run_scan(con, a.drives, scanner=a.scanner, admin=not a.no_admin)
    print(f"Scan #{scan_id} done.")
    return 0


def cmd_top(a: argparse.Namespace) -> int:
    con = db.connect()
    scan_id = db.latest_scan_id(con)
    if scan_id is None:
        print("No completed scan yet. Run: storage-analyzer scan", file=sys.stderr)
        return 1
    q = "SELECT path, allocated, is_dir, files FROM nodes WHERE scan_id=? AND depth<=? "
    args: list = [scan_id, a.depth]
    if a.drive:
        q += "AND drive=? "
        args.append(a.drive.upper().rstrip(":"))
    q += "ORDER BY allocated DESC LIMIT ?"
    args.append(a.n)
    for r in con.execute(q, args):
        kind = "D" if r["is_dir"] else "f"
        print(f"{human(r['allocated']):>9}  {kind}  {r['path']}")
    return 0


def cmd_classify(a: argparse.Namespace) -> int:
    from .analysis import run_classify
    con = db.connect()
    run_classify(con, a.scan)
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    from . import analysis
    con = db.connect()
    scan_id = db.latest_scan_id(con)
    if scan_id is None:
        print("No completed scan yet.", file=sys.stderr)
        return 1
    drive = a.drive.upper().rstrip(":") if a.drive else None
    tot = analysis.totals_by_safety(con, scan_id, drive)
    print(f"Scan #{scan_id}" + (f"  drive {drive}:" if drive else "  all drives"))
    print(f"  SAFE to free : {human(tot.get('safe', 0)):>10}")
    print(f"  REVIEW first : {human(tot.get('review', 0)):>10}")
    print(f"  KEEP         : {human(tot.get('keep', 0)):>10}")
    print(f"  UNKNOWN      : {human(tot.get('unknown', 0)):>10}")
    print("\nBy category:")
    for c in analysis.totals_by_category(con, scan_id, drive):
        print(f"  {c['category']:<10} {human(c['total']):>10}  safe {human(c['safe']):>9}  review {human(c['review']):>9}"
              f"  keep {human(c['keep']):>9}  unknown {human(c['unknown']):>9}  ({c['count']} items)")
    for safety in a.show:
        print(f"\nTop {a.n} {safety.upper()} items:")
        for it in analysis.items(con, scan_id, safety=safety, drive=drive, limit=a.n):
            flag = " [admin]" if it["admin"] else ""
            print(f"  {human(it['excl_bytes']):>9}  {it['path']}{flag}")
            print(f"             {it['rule_id'] or it['source']}: {it['reason']}  →  {it['action']}")
    print("\nProjects:")
    for p in con.execute("SELECT * FROM projects WHERE scan_id=? ORDER BY rebuildable_bytes DESC", (scan_id,)):
        import json
        gi = json.loads(p["git_info"] or "{}")
        git = {"ok": "pushed", "warn": "NOT PUSHED: " + gi.get("summary", ""), "none": "NO GIT", "n/a": "?"}[p["git_state"]]
        print(f"  {human(p['rebuildable_bytes']):>9} rebuildable / {human(p['total_bytes']):>9}  "
              f"{(str(p['stale_days']) + 'd') if p['stale_days'] is not None else '?':>6}  {p['path']}  "
              f"[{','.join(json.loads(p['types']))}]  git: {git}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="storage-analyzer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("drives", help="list local drives").set_defaults(fn=cmd_drives)

    s = sub.add_parser("scan", help="scan drives into the database")
    s.add_argument("--drives", nargs="*", help="drive letters (default: all fixed disks)")
    s.add_argument("--scanner", choices=["auto", "wiztree", "walker"], default="auto")
    s.add_argument("--no-admin", action="store_true", help="skip the UAC prompt (WizTree falls back to a slow scan)")
    s.set_defaults(fn=cmd_scan)

    t = sub.add_parser("top", help="largest items from the latest scan")
    t.add_argument("--drive")
    t.add_argument("-n", type=int, default=30)
    t.add_argument("--depth", type=int, default=3)
    t.set_defaults(fn=cmd_top)

    c = sub.add_parser("classify", help="(re)run rules, project detection and git status on the latest scan")
    c.add_argument("--scan", type=int, help="scan id (default: latest)")
    c.set_defaults(fn=cmd_classify)

    r = sub.add_parser("report", help="totals by safety and category, top items, projects")
    r.add_argument("--drive")
    r.add_argument("-n", type=int, default=25)
    r.add_argument("--show", nargs="*", default=["safe", "review", "unknown"], help="which safety lists to print")
    r.set_defaults(fn=cmd_report)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
