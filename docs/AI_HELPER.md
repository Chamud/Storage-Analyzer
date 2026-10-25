# AI helper (Claude Code)

The rules classify almost everything. What is left is a short list of large folders nobody wrote a rule
for: `C:\ProgramData\Corsair`, `D:\ACCM`, an Electron app's data folder, a client project's `Asset` dump.
Those go to Claude Code, which answers with a category, a safety level, a reason, an action and
optionally a rule to add so it is never asked again.

**The app works without it.** With Claude Code missing or the helper switched off, those folders simply
stay "Unknown" and you decide.

## What is sent

Metadata only, never file contents:

```json
{
  "path": "C:\\ProgramData\\Corsair",
  "allocated_gb": 2.3, "files": 1840, "folders": 120,
  "newest": "2026-09-30", "oldest": "2024-11-02",
  "ext_mix": {".dll": 0.42, ".pak": 0.31, ".json": 0.1},
  "children": [["iCUE", 1.9e9], ["CUE", 3.1e8], ["Logs", 9e7]],
  "markers": [], "installed_match": "Corsair iCUE 5"
}
```

`installed_match` is a fuzzy match against the Windows installed-programs list, so the model can tell
"leftover from an uninstalled app" from "data of an app you still use".

## How it is called

```
claude -p "<instructions + JSON batch>" --output-format json --model <SA_AI_MODEL, default sonnet> \
       --tools "" --max-turns 1
```

- Uses your existing Claude Code login; no API key in this project.
- No tools: the model cannot read, run or delete anything. It can only answer.
- Batches of 10 folders per call, 3 calls in flight at once; the reply is validated against a JSON schema.
- Each answer is cached in `ai_cache` (path + size/mtime/count fingerprint). A rescan does not re-ask; a
  folder that merely changed size (±50%, e.g. after its cache was cleaned) reuses the answer for 30 days.
- Speed is dominated by output length and CLI start-up, not by the model: Opus is *slower* than Sonnet,
  Haiku is faster. `SA_AI_MODEL=haiku` for a quick pass.

## What the model may say

| field | values | enforced |
|---|---|---|
| `category` | any key from `categories` | unknown keys → `unknown` |
| `safety` | `review` or `keep` | `safe` is **downgraded to `review`**; the AI never makes something auto-deletable |
| `confidence` | 0–1 | shown as "AI · 82% sure" |
| `reason`, `action` | one sentence each | shown verbatim with an "AI" badge |
| `suggested_rule` | a rule in `rules/user.yaml` format, or null | shown; appended only when you accept it |

Rules always win: a folder a rule marked `keep` is never sent to the AI.

## Git judgement

For projects without git, the rules already decide "should have git" from marker files. The AI is only
asked about folders under a project root that have source-like extensions but no marker, e.g. a bare
folder of `.cs` files. Same metadata, same caps.

## Running it

```powershell
storage-analyzer ai              # classify all unknown roots ≥ 100 MB not yet cached
storage-analyzer ai --dry-run    # print the batch that would be sent
storage-analyzer ai --model haiku
```

In the UI: the "AI helper" toggle and "Ask AI" buttons call the same code.

## Alternative: Claude Agent SDK

If this is ever shipped to other people, replace `claude -p` with the Python Agent SDK and an API key; the
prompt, batch shape and caps stay the same (`ai/claude_code.py` is the only file that changes).
