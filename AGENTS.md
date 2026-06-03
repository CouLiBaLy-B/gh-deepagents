# AGENTS.md — instructions persistantes pour les Deep Agents

Ce fichier est lu automatiquement par les Deep Agents au démarrage et sert de
mémoire de projet long-terme (équivalent du `CLAUDE.md` de Claude Code).

## Conventions du projet gh-deepagent
- Code Python ≥ 3.11, formaté avec `ruff format`.
- Type hints partout (`from __future__ import annotations`).
- Pas de dépendances lourdes additionnelles sans justification.
- Tests sous `tests/`, exécutés via `pytest -q`.

## Conventions pour les repos *cibles* (que l'agent doit modifier)
- Lis toujours le `AGENTS.md` ou `CONTRIBUTING.md` du repo avant de coder.
- Détecte le test runner via les fichiers présents (`pyproject.toml`,
  `package.json`, `go.mod`, `Cargo.toml`, `Makefile`).
- Si un linter est configuré (ruff, eslint, gofmt, clippy), passe-le avant
  `finalize_patch`.

## Workflow par défaut
1. `write_todos` — décompose la tâche
2. `list_project_files` + `read_file` — orientation
3. édition + `run_tests` (boucle jusqu'à vert)
4. `summarize_diff` — sanity check
5. `finalize_patch` — commit / push / PR (une seule fois)
