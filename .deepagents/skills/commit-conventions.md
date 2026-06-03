# Skill: commit-conventions

Always write commits in **Conventional Commits** format.

```
<type>(<scope>): <subject>

<body>

<footer>
```

## Types
| Type      | When to use                                                  |
|-----------|--------------------------------------------------------------|
| `feat`    | New user-facing capability                                   |
| `fix`     | Bug fix                                                      |
| `refactor`| Internal restructuring, no behaviour change                  |
| `perf`    | Performance improvement                                      |
| `test`    | Only adds/changes tests                                      |
| `docs`    | Doc / comment only                                           |
| `style`   | Formatting, whitespace, no code logic change                 |
| `build`   | Build system, dependency bump                                |
| `ci`      | CI / GitHub Actions only                                     |
| `chore`   | Tooling, scripts, no production code                         |
| `revert`  | Reverts a previous commit                                    |

## Rules
- Subject: imperative ("add X", not "added X"), ≤ 72 chars, no trailing dot.
- Body: explain **why**, not what (the diff shows what).
- One logical change per commit; if you need "and" in the subject, split it.
- Reference the issue in the footer: `Refs #123` or `Closes #123`.
- Add `BREAKING CHANGE:` footer if public API changes; bump major.

## Examples
```
feat(api): add /healthz endpoint returning service version

Used by k8s readiness probes. Returns 200 with JSON body
{"status":"ok","version":"..."} once the DB pool is reachable.

Closes #142
```

```
fix(parser): handle empty input without crashing

Previously raised IndexError on []. Now returns None and logs a warning.

Refs #200
```
