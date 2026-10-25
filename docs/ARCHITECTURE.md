# Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Web UI  (storage_analyzer/web/static)  — one HTML page, no build    │
│  Overview · Projects · Categories · Explore map · Cleanup            │
└───────────────▲──────────────────────────────┬───────────────────────┘
                │ JSON                          │ POST /api/cleanup
┌───────────────┴──────────────────────────────▼───────────────────────┐
│  FastAPI  (web/app.py)        actions.py: recycle / command / manual │
└───────────────▲──────────────────────────────────────────────────────┘
                │ reads                                   ▲ writes
┌───────────────┴──────────────────────────────────────────────────────┐
│  SQLite  data/storage.db                                             │
│  scans · drives · nodes · classifications · projects · ai_cache ·    │
│  cleanup_log · settings                                              │
└───▲────────────────▲───────────────────▲────────────────▲────────────┘
    │ importer       │ rules.classify    │ projects       │ ai.apply
┌───┴────────┐  ┌────┴─────────┐  ┌──────┴───────┐  ┌─────┴──────────┐
│ scanner/   │  │ rules/*.yaml │  │ git, markers │  │ claude -p      │
│ wiztree.py │  │ rules.py     │  │ projects.py  │  │ ai/claude_code │
│ walker.py  │  └──────────────┘  └──────────────┘  └────────────────┘
└────────────┘
```

## Pipeline

1. **scan** (`scan.py`): lists fixed drives (Win32 API), runs the scanner, imports.
   - `scanner/wiztree.py` writes a `.ps1` that runs `WizTree64.exe <drive> /export=… /admin=1` for each
     drive and launches it **once, elevated** (`Start-Process -Verb RunAs`). One UAC prompt, ~10 s per drive.
     Then it parses the CSV (folders + every file).
   - `scanner/walker.py` is the fallback: recursive `os.scandir`, no admin, minutes per drive.
   - `scanner/importer.py` streams entries into `nodes`, resolving `parent_id` from a path→id map (both
     scanners emit parents before children). Files under `MIN_FILE_BYTES` (1 MB) are not stored; their bytes
     are already in the folder totals WizTree reports.
2. **classify** (`analysis.run_classify`):
   - `projects.detect` finds project roots (marker folders in the DB + marker files on disk under
     `project_roots`), skipping anything inside caches, AppData, Program Files or a user-profile root.
   - `rules.classify` walks all nodes in pre-order with the rule set. Each matched node becomes a *root*
     with a category + safety; `excl_bytes` = allocated − nested roots, so sums by category never double
     count. See [RULES.md](RULES.md) for the semantics of `keep` / `container` / `rebuildable` / unknown.
   - `projects.analyse` reads git status (8 threads), computes staleness (newest non-build folder mtime or
     last commit), resolves `rebuildable` roots to `safe` (stale project) or `review` (active).
3. **ai** (optional, [AI_HELPER.md](AI_HELPER.md)): large `unknown` roots are summarised (metadata only) and
   sent to `claude -p` in batches; answers are cached in `ai_cache` and written as `source='ai'`.
4. **serve**: FastAPI reads the tables; the UI never touches the filesystem directly.
5. **cleanup** (`actions.py`): plans actions for selected node ids, refuses protected paths, executes
   (Recycle Bin via `send2trash`; admin-only paths through one elevated helper process; whitelisted
   commands; manual steps open the right tool) and logs everything.

## Data model notes

- `nodes.path` has no trailing backslash except drive roots (`C:\`). Compare case-insensitively.
- Descendants of a path are selected with `path > 'P\' AND path < 'P]'` (`]` follows `\` in ASCII), which
  uses the `(scan_id, path)` index. Never use `LIKE` for this: it is case-insensitive and skips the index.
- `classifications` holds roots only. Everything else inherits from its nearest classified ancestor.
- `ai_cache` survives rescans: keyed by path + fingerprint (allocated, mtime, file count).

## Why these choices

- **WizTree over our own MFT reader**: weeks of work for no better numbers. The CSV has everything the
  rules need. The walker exists so the app still works without it.
- **SQLite over in-memory**: 500 K rows, instant UI loads, rescans can be diffed later.
- **Rules first, AI second**: deterministic, explainable, offline. The AI only sees what rules could not
  name, which on a typical machine is a few dozen folders.
- **Single HTML page**: no Node toolchain needed to hack on the UI.
