"""Download the official WizTree portable zip and unpack it into tools/wiztree/.

WizTree may not be redistributed (see its EULA), so it is never committed to
this repository. Run this once after cloning:

    python scripts/get_wiztree.py

WizTree is free for PERSONAL use only. For business use buy a supporter code
from https://diskanalyzer.com/donate.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "tools" / "wiztree"
VERSION = "4_33"
URL = f"https://diskanalyzer.com/files/wiztree_{VERSION}_portable.zip"


def download(url: str) -> bytes:
    print(f"Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "storage-analyzer/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https URL
        total = int(resp.headers.get("Content-Length") or 0)
        buf = io.BytesIO()
        read = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            buf.write(chunk)
            read += len(chunk)
            if total:
                print(f"\r  {read / 1e6:5.1f} / {total / 1e6:.1f} MB", end="", flush=True)
        print()
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=URL, help="override the zip URL (e.g. a newer version)")
    ap.add_argument("--force", action="store_true", help="re-download even if WizTree64.exe exists")
    args = ap.parse_args()

    exe = DEST / "WizTree64.exe"
    if exe.exists() and not args.force:
        print(f"Already present: {exe}  (use --force to re-download)")
        return 0

    data = download(args.url)
    print(f"  sha256 {hashlib.sha256(data).hexdigest()}")

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        if "WizTree64.exe" not in names:
            print("Zip does not contain WizTree64.exe; refusing to unpack", file=sys.stderr)
            return 1
        DEST.mkdir(parents=True, exist_ok=True)
        for member in zf.infolist():
            # Guard against zip paths escaping the destination folder.
            target = (DEST / member.filename).resolve()
            if DEST.resolve() not in target.parents and target != DEST.resolve():
                print(f"Skipping suspicious path in zip: {member.filename}", file=sys.stderr)
                continue
            zf.extract(member, DEST)
    print(f"Unpacked to {DEST}")
    print("Reminder: WizTree is free for personal use only (tools/wiztree/license.txt).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
