---
name: github-workflow
description: "GitHub branch/PR workflow conventions (branch naming, PR body sections, linking issues). Use when opening or updating a PR or naming a branch."
license: MIT
---

# Skill: github-workflow

Use this skill when working on any task that targets a GitHub repository.

## Branch naming
- Issues: `deepagent/issue-<NUMBER>-<short-slug>`
- Evolutions: `deepagent/evolve-<timestamp>-<short-slug>`
- Hotfixes: `deepagent/hotfix-<short-slug>`

## Commit messages
Follow Conventional Commits:
```
<type>(<scope>): <subject>

<body>

Refs #<issue>
```
Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `ci`.

## PR body template
```markdown
## Plan
- [x] step 1
- [x] step 2

## Changes
- file/path.py — why

## Tests
```
$ pytest -q
...
```

Closes #<issue>
```

## Hard rules
1. Never push to default branch.
2. Never delete files unless explicitly asked.
3. Never commit secrets, `.env`, or large binaries.
4. If pre-commit / lint config exists, run it before `finalize_patch`.
