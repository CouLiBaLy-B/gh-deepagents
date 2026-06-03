# Skill: migration-playbook

Use when a change must be applied **mechanically** across many files (renames,
API swaps, import path changes, deprecation removals, framework upgrades).

## The 3 tiers — pick the smallest that works

| Tier | Tool                        | Use for                                              |
|------|-----------------------------|------------------------------------------------------|
| 1    | `ast_grep_rewrite`          | 90% of cases. Pure structural pattern + replacement. |
| 2    | `codemod_python` (libcst)   | Need cross-file context, decorator inspection, type. |
| 3    | Hand-edit via `coder`       | A handful of irregular sites that resist patterns.   |

Always start at tier 1. Drop to tier 2 only if the dry-run shows tier 1 can't
capture the variants. Drop to tier 3 only for the residual <10 sites.

## Universal workflow

```
ast_grep_search(pattern)             ← count first
        │
        ▼
ast_grep_rewrite(apply=False)        ← dry-run, inspect diff
        │
        ▼   (refine pattern if diff is wrong)
ast_grep_rewrite(apply=True)         ← write
        │
        ▼
format_code()                        ← re-style
        │
        ▼
run_tests("pytest -q path/to/most-touched-module")    ← fast feedback
        │
        ▼
run_tests()                          ← full suite
```

## Pattern recipes

### Function rename
```
pattern:  old_name($$$ARGS)
rewrite:  new_name($$$ARGS)
lang:     python
```

### Argument addition with default
```
pattern:  create_user($NAME, $EMAIL)
rewrite:  create_user($NAME, $EMAIL, source="legacy")
```

### Import path migration
```
pattern:  from old.pkg import $WHAT
rewrite:  from new.pkg import $WHAT
```

### API swap with method rename (requests → httpx sync)
```
pattern:  requests.get($URL, $$$KW)
rewrite:  httpx.get($URL, $$$KW)
```
(Then add an import-fix pass.)

### Deprecation removal (decorator)
```
pattern: |
  @deprecated($$$)
  def $NAME($$$ARGS):
    $$$BODY
rewrite: |
  def $NAME($$$ARGS):
    $$$BODY
```

## When to use libcst (codemod_python)

- Need to remove an argument that's positional in some calls, keyword in others.
- Need to inspect the surrounding class hierarchy.
- Need to add an import only if a symbol is used after rewrite.
- Need to preserve specific formatting (libcst is concrete-syntax, ast-grep is not).

## Exclusions (never touch)

- `*_pb2.py`, `*_pb2_grpc.py` (protobuf-generated)
- `*.generated.*`, `*.gen.*`
- `dist/`, `build/`, `node_modules/`, `vendor/`
- `migrations/`, `alembic/versions/` (manage with the framework's tool, not codemods)
- Test snapshots (`__snapshots__/`, `*.snap`)

## Risk gates

- < 50 sites: proceed.
- 50–200 sites: split per top-level directory, test in between.
- \> 200 sites: confirm with the lead first. Split into multiple PRs if possible.

## When to abort

- Dry-run diff shows >10% false positives → refine pattern or drop to libcst.
- Tests broken after rewrite + format + 2 retry rounds → revert, propose
  manual migration plan to the lead.
- Generated files appear in the diff → fix the exclusion glob, redo.
