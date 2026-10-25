# Storage Analyzer

Finds what is using your disk space, sorts it into **safe to delete / review first / keep**, and cleans it up
with one click. Built around [WizTree](https://diskanalyzer.com/) for scanning (seconds per drive) and an
optional [Claude Code](https://claude.com/claude-code) helper for the folders no rule recognises.

Works fully without AI: deterministic YAML rules classify ~95% of what matters. The AI only looks at large
folders the rules could not name, only sees metadata (never file contents), and can only *suggest*. Nothing
is ever deleted without you ticking it.

## What it does

| View | Purpose |
|---|---|
| **Overview** | Per-drive bars, "you can safely free X GB", quick wins with checkboxes, git/backup warnings |
| **Projects** | Every code project found, last-touched date, git status (pushed / not pushed / no git), and the `.venv`, `node_modules`, `build`, `Library`… folders you can rebuild |
| **Categories** | Dev caches, package caches, AI models, app caches, temp, downloads, games, containers, system, personal, unknown |
| **Explore map** | WizTree-style treemap, coloured by *safety* instead of file type |
| **Cleanup** | Cart of selected items, total to free, how each is cleaned (Recycle Bin / built-in command / "do it in the app"), dry run, log |

## Quick start

```powershell
git clone <this repo> "Storage Analyzer"
cd "Storage Analyzer"
uv venv && uv pip install -e .          # or: python -m venv .venv; .venv\Scripts\pip install -e .
python scripts/get_wiztree.py           # downloads the official WizTree portable zip into tools/wiztree/
.venv\Scripts\storage-analyzer scan     # one Windows admin prompt, then seconds per NTFS drive
.venv\Scripts\storage-analyzer top      # sanity check: largest items
.venv\Scripts\storage-analyzer serve    # opens the UI at http://127.0.0.1:8765
```

No WizTree? `storage-analyzer scan --scanner walker` uses a built-in Python walk (minutes instead of
seconds, no admin prompt).

## How it works

```
scan  →  WizTree CSV (or os.scandir)  →  SQLite nodes table (folders + files ≥ 1 MB)
      →  rules/default.yaml + rules/user.yaml   →  category + safety per folder
      →  project detection (.git, pyproject.toml, package.json, *.sln, Unity…)  →  stale days, git status
      →  [optional] claude -p for large Unknown folders  →  cached in SQLite, never asked twice
serve →  FastAPI + a single-page UI  →  cleanup via Recycle Bin / native commands, everything logged
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/RULES.md](docs/RULES.md),
[docs/AI_HELPER.md](docs/AI_HELPER.md) and [docs/WIZTREE.md](docs/WIZTREE.md).

## Safety model

- **Keep** items (Windows, Program Files, page file, your documents, repos with unpushed work) have no
  checkbox. Rules for them always win over the AI.
- AI answers are capped at **Review**; the AI can never mark something Safe on its own.
- Deletes go to the **Recycle Bin** by default. Where an app has its own cleanup (`docker system prune`,
  `pip cache purge`, `powercfg /h off`, Unity Hub, Steam) the app runs or opens that instead of deleting
  raw files.
- Only the scan runs elevated (WizTree needs it for MFT mode). The UI, the AI and deletions run as you.
- Every cleanup action is logged in `data/storage.db` (`cleanup_log`).

## Licensing

This project is MIT. **WizTree is not included**: it is free for personal use only and may not be
redistributed, so `scripts/get_wiztree.py` fetches it from the official site into the git-ignored
`tools/` folder. Business use of WizTree needs a licence from Antibody Software.
