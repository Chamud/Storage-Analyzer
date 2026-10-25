# Rules

Rules live in `rules/default.yaml` (built-in) and `rules/user.yaml` (yours; wins over default). Re-run with
`storage-analyzer classify` after editing; no rescan needed.

## A rule

```yaml
- id: gradle-caches                    # unique; shown in the UI as "why"
  match:
    path: ["~\\.gradle\\caches"]       # glob(s) on the full path
    name: ["node_modules"]             # glob(s) on the last path segment (any depth)
    ext: [".iso", ".img"]              # file extension(s); implies files
    under: "~\\Downloads"              # only below this path
    is_dir: true                       # true / false / omitted
    min_bytes: 104857600               # ignore smaller items
    in_project: true                   # only inside a detected project...
    project_types: [unity]             # ...of these types (python, node, dotnet, java, unity, unreal, rust, go, cmake)
  category: pkg                        # key from `categories`
  safety: safe                         # safe | review | keep | container | rebuildable
  reason: "Why this is what it is."    # one sentence, shown to the user
  action: "What to do about it."       # one sentence
  method: recycle                      # recycle (default) | command | manual | none
  command: "pip cache purge"           # for command/manual; commands are whitelisted by appearing here
  admin: true                          # needs elevation
  override: true                       # may apply inside a `keep` subtree
  priority: 5                          # higher wins; default 0; user.yaml adds +1000
```

All `match` keys given must hold (AND). Lists inside a key are alternatives (OR).

## Globs

| Token | Meaning |
|---|---|
| `*` | anything within one path segment |
| `**` | anything, across segments |
| `?` | one character (`?:\` = any drive) |
| `~` | any user profile (`?:\Users\*`) |
| `%LOCALAPPDATA%` `%APPDATA%` `%TEMP%` `%PROGRAMDATA%` | the usual folders, for any user |

Matching is case-insensitive and anchored (the whole path must match).

## Safety semantics

| safety | Meaning | Subtree |
|---|---|---|
| `safe` | Delete/clean without thinking. Re-downloadable, rebuilt automatically, or already deleted. | not classified further |
| `review` | Probably fine, but the user decides (games, installers, model weights, active-project builds). | not classified further |
| `keep` | Never offer for deletion: system, installed programs, personal data, source code. | protected; only rules with `override: true` apply inside |
| `container` | Not an item itself: look at its children. | children that match nothing and are ≥ `SA_UNKNOWN_MIN_BYTES` (100 MB) become **unknown** roots |
| `rebuildable` | Build output of a project. Resolved after project analysis: `safe` if the project is stale (≥ `SA_STALE_DAYS`, 90), else `review`. | not classified further |

Detected **project roots** are implicitly `keep` (category `projects`); the `in_project` dev rules carry
`override: true` so build folders inside them are still found. Folders that *contain* a project somewhere
below are treated as containers rather than reported as one big unknown.

Rules are tried in order of `priority` (desc), then file order. The first match wins. Put specific rules
above generic ones, or give the generic one a negative priority (see `generic-app-cache`).

## Adding your own

`rules/user.yaml`:

```yaml
rules:
  - id: my-renders
    match: { path: "D:\\Renders\\**", is_dir: true, min_bytes: 1073741824 }
    category: media
    safety: review
    reason: "Blender renders. Re-renderable from the .blend files in OneDrive."
    action: "Delete to Recycle Bin"
```

The AI helper proposes rules in the same format; accepting one appends it here.

## Checking a rule

```powershell
storage-analyzer classify
storage-analyzer report --show safe review unknown -n 40
```

The report prints, for every item, the rule id that matched. If something is wrong, a more specific rule
with a higher `priority` fixes it without touching the default file.
