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

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
