# Architecture interne

## Vue d'ensemble

`gh-deepagent` est une fine couche au-dessus de [`deepagents`](https://github.com/langchain-ai/deepagents).
Deep Agents fournit déjà :

- **planning** (`write_todos`), inspiré de Claude Code
- **filesystem backend** (lecture/écriture/édition de fichiers)
- **subagents** (délégation à contexte isolé via le tool `task`)
- **context management** (offloading dans des fichiers virtuels)
- **HITL middleware** (approbation de tools sensibles)

Nous ajoutons par-dessus :

1. **`LocalShellBackend(root_dir=repo)`** — restreint l'agent au repo cloné et lui
   donne `bash`, `git`, `pytest`, etc.
2. **Tools GitHub spécifiques** (`fetch_issue`, `run_tests`, `summarize_diff`,
   `finalize_patch`).
3. **Sous-agents métier** : `coder`, `tester`, `reviewer`.
4. **Glue GitHub Actions** (`gh-deepagent github-event`) qui dispatch sur `issues` /
   `issue_comment` / `workflow_dispatch`.

## Cycle de vie d'une exécution

```
fix_issue(url)
   │
   ├── GitHubOps.fetch_issue_context()    # PyGithub
   ├── GitHubOps.clone(repo, /tmp/...)    # GitPython + token URL
   ├── build_agent(repo_path, ...)        # crée le Deep Agent
   │      └── create_deep_agent(model, tools, backend=LocalShell, subagents=[...])
   ├── agent.stream({"messages": [...]})  # boucle planning → édition → tests
   │      └── finalize_patch() côté LLM   # commit + push + open_pr
   └── return RunResult(pr_url=...)
```

## Sécurité

| Risque                                  | Mitigation                                              |
|-----------------------------------------|---------------------------------------------------------|
| Exécution shell arbitraire              | `LocalShellBackend(root_dir=repo)` cloisonne au repo    |
| Push direct sur `main`                  | Prompt + branch policy + GitHub branch protection       |
| Fuite de secrets dans le diff           | Pré-commit hook conseillé (`gitleaks`)                  |
| Boucle infinie / coût                   | `DEEPAGENT_MAX_TURNS` + `concurrency` dans Actions      |
| Tool calls dangereux (drop tables...)   | HITL via `--interactive` / `DEEPAGENT_INTERACTIVE=1` (approbation console avant `finalize_patch`/`codemod_python`/`ast_grep_rewrite`) |
| Codemod LLM exécuté en clair            | `codemod_python` tourne dans un sous-processus isolé (env scrubé, timeout) |

## Pourquoi `LocalShellBackend` plutôt qu'un sandbox distant ?

Deep Agents supporte aussi `DaytonaSandbox`, `ModalSandbox`, `RunloopSandbox`, etc.
Pour une exécution locale ou dans GitHub Actions, le `LocalShellBackend` est le plus
simple et le moins cher. Si tu veux isoler totalement (repos non-confiance), bascule
sur `DaytonaSandbox` en deux lignes :

```python
from deepagents.backends import DaytonaSandbox
backend = await DaytonaSandbox.create(...)
```

## Pourquoi pas `open-swe` directement ?

[`open-swe`](https://github.com/langchain-ai/open-swe) est le grand frère hébergé,
asynchrone, multi-sandbox. `gh-deepagent` vise un usage plus léger : un binaire
Python, configurable en 30 secondes, qui marche en CLI **et** dans Actions sans
infrastructure additionnelle. Si tes besoins grossissent, migre vers open-swe.
