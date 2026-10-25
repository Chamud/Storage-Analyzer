"""Orchestrates a scan: pick drives, run a scanner, import into SQLite, record drive totals."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from . import config
from .scanner import list_drives
from .scanner import walker as walker_mod
from .scanner import wiztree as wiztree_mod
from .scanner.importer import import_entries


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def run_scan(con: sqlite3.Connection, letters: list[str] | None = None, scanner: str = "auto",
             admin: bool = True, log=print) -> int:
    drives = list_drives(fixed_only=True)
    if letters:
        wanted = {L.upper().rstrip(":\\") for L in letters}
        drives = [d for d in drives if d.letter in wanted]
        missing = wanted - {d.letter for d in drives}
        if missing:
            raise ValueError(f"Drive(s) not found or not fixed disks: {', '.join(sorted(missing))}")
    if not drives:
        raise ValueError("No fixed drives found")

    if scanner == "auto":
        scanner = "wiztree" if wiztree_mod.is_available() else "walker"
        if scanner == "walker":
            log("WizTree not found; using the slower built-in walker. Run scripts/get_wiztree.py for fast scans.")

    cur = con.execute("INSERT INTO scans(started_at, scanner) VALUES(?,?)", (_now(), scanner))
    scan_id = cur.lastrowid
    log(f"Scan #{scan_id} with {scanner}: {', '.join(d.letter + ':' for d in drives)}")

    try:
        for d in drives:
            con.execute("INSERT INTO drives(scan_id,letter,label,fs,capacity,free,used) VALUES(?,?,?,?,?,?,?)",
                        (scan_id, d.letter, d.label, d.fs, d.capacity, d.free, d.used))

        if scanner == "wiztree":
            ntfs = [d.letter for d in drives if d.fs.upper() == "NTFS"]
            other = [d.letter for d in drives if d.fs.upper() != "NTFS"]
            if other:
                log(f"Note: {', '.join(other)} are not NTFS; WizTree will scan them the slow way.")
            log("Starting WizTree. Approve the Windows admin prompt for fast MFT mode.")
            csvs = wiztree_mod.run_export(ntfs + other, admin=admin)
            for d in drives:
                totals, entries = wiztree_mod.parse_csv(csvs[d.letter])
                log(f"Importing {d.letter}: ...")
                stats = import_entries(con, scan_id, d.letter, entries,
                                       on_progress=lambda n, L=d.letter: log(f"  {L}: {n:,} rows", end="\r"))
                log(f"  {d.letter}: {stats['dirs']:,} folders, {stats['files']:,} files >= "
                    f"{config.MIN_FILE_BYTES // 1024 // 1024} MB kept, "
                    f"{stats['skipped_small_files']:,} small files folded into folders ({stats['seconds']}s)")
        else:
            for d in drives:
                log(f"Walking {d.letter}: (this takes a few minutes)")
                entries = walker_mod.walk_drive(d.letter, config.MIN_FILE_BYTES,
                                                on_progress=lambda n, p, L=d.letter: log(f"  {L}: {n:,} seen", end="\r"))
                stats = import_entries(con, scan_id, d.letter, entries)
                log(f"  {d.letter}: {stats['dirs']:,} folders, {stats['files']:,} big files ({stats['seconds']}s)")

        con.execute("UPDATE scans SET finished_at=?, status='done' WHERE id=?", (_now(), scan_id))
        return scan_id
    except BaseException as ex:
        con.execute("UPDATE scans SET finished_at=?, status='failed', note=? WHERE id=?", (_now(), str(ex), scan_id))
        raise
