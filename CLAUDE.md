# CLAUDE.md — working on Storage Analyzer

Windows-only disk space analyser: scan with WizTree → SQLite → YAML rules → optional Claude Code helper →
local web UI → cleanup via Recycle Bin / native commands. Python 3.12, FastAPI, no JS build step.

## Set up from a clean clone

```powershell
cd "Storage Analyzer"
uv venv --python 3.12                  # or: py -3.12 -m venv .venv
uv pip install -e ".[dev]"             # or: .venv\Scripts\pip install -e ".[dev]"
python scripts/get_wiztree.py          # fetches WizTree portable into tools/wiztree/ (git-ignored, see below)
.venv\Scripts\storage-analyzer drives  # smoke test: lists local drives
```

Then `.venv\Scripts\storage-analyzer scan` (one UAC prompt; ~30 s for two NTFS drives) and
`.venv\Scripts\storage-analyzer serve` for the UI. Use `scan --scanner walker` when WizTree is not
available (slow, no admin).

Requirements: Windows 10/11, Python 3.12+, `uv` recommended, `git` on PATH (for project git status),
Claude Code CLI on PATH only if the AI helper is wanted (`claude --version`).

## Layout

```
storage_analyzer/
  config.py        paths + tunables (env overrides: SA_DATA_DIR, SA_WIZTREE_EXE, SA_MIN_FILE_BYTES, SA_STALE_DAYS)
  db.py            SQLite schema + connect(); WAL mode; every table keyed by scan_id
  scan.py          run_scan(): pick drives → scanner → importer → drive totals
  scanner/         __init__ (Entry, list_drives), wiztree.py (export + CSV parse), walker.py (fallback), importer.py
  rules.py         load rules/*.yaml, match nodes, write classifications
  projects.py      detect code projects, staleness, git status, rebuildable folders
  ai/              claude_code.py: `claude -p` runner with read-only MCP tools; cache in ai_cache
  actions.py       cleanup: recycle / command / manual; logs to cleanup_log
  web/             app.py (FastAPI) + static/ (single page UI)
  cli.py           drives | scan | classify | top | report | serve
rules/default.yaml built-in rules; rules/user.yaml your overrides (committed; no secrets)
scripts/           get_wiztree.py
docs/              ARCHITECTURE.md, RULES.md, AI_HELPER.md, WIZTREE.md
tools/             git-ignored third-party binaries (WizTree)
data/              git-ignored: storage.db, exports/*.csv, logs/
```

## Rules of the house

- **Licence is PolyForm Noncommercial 1.0.0** (LICENSE) with paid commercial use (COMMERCIAL-LICENSE.md).
  Do not change it to a permissive licence, and do not copy GPL code into the project.
- **Never commit `tools/` or `data/`.** WizTree's EULA forbids redistribution; the DB holds the user's
  full file listing.
- **Only the scan is elevated.** WizTree is launched through one elevated PowerShell (`scanner/wiztree.py`);
  the server, the AI and deletions run as the normal user. Do not add `-Verb RunAs` anywhere else.
- **Safety invariants** (enforced in `rules.py`/`actions.py`, keep them that way):
  - The AI can never produce `safe`: its answers are `review` or `keep` (a `safe` reply is downgraded).
    Only rules and the user make something auto-deletable.
  - A `keep` root protects its whole subtree unless a rule sets `override: true`.
  - Deleting goes through `send2trash` (Recycle Bin) by default; permanent delete needs an explicit flag.
  - Any path under a `keep` root, any drive root, and any project with unpushed git work is refused by `actions.py`.
- **AI gets metadata only**: path, sizes, counts, dates, extension mix, child names. Never file contents.
- Node paths are stored **without** a trailing backslash (except drive roots `C:\`). Compare case-insensitively.
- Sizes: `allocated` is what the UI shows and what "space freed" means; `size` is logical bytes.
- Scanners yield **pre-order** (parent before child); the importer depends on it.

## Working on it

- Run tests: `.venv\Scripts\pytest`. Lint: `.venv\Scripts\ruff check .`
- Add a rule: edit `rules/default.yaml` (format in `docs/RULES.md`), then `storage-analyzer classify`
  re-runs classification on the latest scan without rescanning.
- Inspect data quickly: `storage-analyzer top --drive C -n 40 --depth 3`, or open `data/storage.db` with
  any SQLite tool.
- Commits: small, one topic each; conventional subject line, body says *why*.
- Keep docs in `docs/` in sync when changing the rules format, the DB schema or the AI contract.
