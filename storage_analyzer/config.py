"""Paths and tunables. Everything lives inside the project folder unless overridden by env vars."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TOOLS_DIR = ROOT / "tools"
WIZTREE_EXE = Path(os.environ.get("SA_WIZTREE_EXE", TOOLS_DIR / "wiztree" / "WizTree64.exe"))

DATA_DIR = Path(os.environ.get("SA_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "storage.db"
EXPORT_DIR = DATA_DIR / "exports"
LOG_DIR = DATA_DIR / "logs"

RULES_DIR = ROOT / "rules"
DEFAULT_RULES = RULES_DIR / "default.yaml"
USER_RULES = RULES_DIR / "user.yaml"

# Files smaller than this are not stored individually; their size still counts in their folder.
MIN_FILE_BYTES = int(os.environ.get("SA_MIN_FILE_BYTES", 1 * 1024 * 1024))

# A project whose newest source file is older than this is "stale".
STALE_DAYS = int(os.environ.get("SA_STALE_DAYS", 90))


def ensure_dirs() -> None:
    for d in (DATA_DIR, EXPORT_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
