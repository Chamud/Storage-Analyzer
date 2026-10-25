"""Scanners produce a stream of Entry objects; the importer writes them to SQLite.

Two scanners exist:
  * wiztree  - runs WizTree64.exe (elevated, reads the NTFS MFT: seconds per drive) and parses its CSV
  * walker   - plain os.scandir walk in Python; slower (minutes) but needs nothing installed
"""

from __future__ import annotations

import ctypes
import string
from dataclasses import dataclass


@dataclass(slots=True)
class Entry:
    path: str          # full path; folders without trailing backslash, drive roots as 'C:\\'
    is_dir: bool
    size: int
    allocated: int
    mtime: str | None  # ISO 8601
    attrs: int
    files: int         # recursive counts; 0 for files
    folders: int


@dataclass(slots=True)
class DriveInfo:
    letter: str
    label: str
    fs: str
    capacity: int
    free: int

    @property
    def used(self) -> int:
        return self.capacity - self.free


DRIVE_FIXED = 3


def list_drives(fixed_only: bool = True, ntfs_only: bool = False) -> list[DriveInfo]:
    """Local drives via the Win32 API. By default only fixed disks (no USB sticks / card readers)."""
    k32 = ctypes.windll.kernel32
    out: list[DriveInfo] = []
    mask = k32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        root = f"{letter}:\\"
        dtype = k32.GetDriveTypeW(root)
        if fixed_only and dtype != DRIVE_FIXED:
            continue
        label = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        ok = k32.GetVolumeInformationW(root, label, 261, None, None, None, fs, 261)
        if not ok:
            continue
        if ntfs_only and fs.value.upper() != "NTFS":
            continue
        free = ctypes.c_ulonglong()
        total = ctypes.c_ulonglong()
        k32.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), ctypes.byref(free))
        out.append(DriveInfo(letter, label.value, fs.value, total.value, free.value))
    return out
