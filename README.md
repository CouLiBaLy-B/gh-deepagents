# gh-deepagent

Système de **codage automatique** et de **résolution d'issues GitHub** propulsé par
[Deep Agents](https://github.com/langchain-ai/deepagents) (LangChain / LangGraph), conçu
pour fonctionner :

- **en local** via une CLI (`gh-deepagent fix <issue-url>`),
- **dans GitHub Actions** sur `issues`, `issue_comment` et `workflow_dispatch`,
- avec un **LLM local** (Ollama : `qwen2.5-coder`, `llama3.1`, etc.) ou n'importe quel
  provider supporté par LangChain.

## Ce qu'il sait faire

| Cas d'usage                                       | Trigger                                              | Sortie                 |
|---------------------------------------------------|------------------------------------------------------|------------------------|
| Résoudre une issue bug / feature                  | label `deepagent` sur l'issue                        | nouvelle branche + PR  |
| Traiter une demande d'évolution (nouveau code)    | commentaire `/deepagent <instruction>` sur une issue | nouvelle branche + PR  |
| **Itérer sur une PR existante** (nouveau)         | commentaire `/deepagent <instruction>` sur une PR    | commit additionnel     |
| Travailler en local sur ta machine                | `gh-deepagent fix <url>`                             | diff appliqué + PR     |
| Revue automatique de PR                           | label `deepagent-review` sur la PR                   | review commentée       |
| **Webhook auto-hébergé** (nouveau)                | GitHub App → `POST /webhook`                         | identique aux ci-dessus|
| **Sandbox isolé** (nouveau)                       | `DEEPAGENT_BACKEND=daytona|modal|runloop`            | exécution hors host    |

## Architecture

```
                    ┌─────────────────────────────┐
                    │   GitHub Issue / Comment    │
                    │   ou CLI locale             │
                    └──────────────┬──────────────┘
                                   │
                  (workflow_dispatch / issues)
                                   ▼
                    ┌─────────────────────────────┐
                    │   gh-deepagent runner       │
                    │   (Python, headless)        │
                    │                             │
                    │   ┌─────────────────────┐   │
                    │   │ create_deep_agent() │   │  ← LangGraph harness
                    │   │ ─ planning tool     │   │  ← write_todos
                    │   │ ─ filesystem backend│   │  ← LocalShellBackend
                    │   │ ─ subagents         │   │  ← code-writer, tester, reviewer
                    │   │ ─ skills (AGENTS.md)│   │  ← convention du repo
                    │   └─────────────────────┘   │
                    │            │                │
                    │   ┌────────▼──────────┐     │
                    │   │ Tools custom :    │     │
                    │   │ - github_get_issue│     │
                    │   │ - run_tests       │     │
                    │   │ - git_commit/push │     │
                    │   │ - open_pr         │     │
                    │   └───────────────────┘     │
                    └──────────────┬──────────────┘
                                   ▼
                    ┌─────────────────────────────┐
                    │  PR GitHub avec patch +     │
                    │  résumé du plan exécuté     │
                    └─────────────────────────────┘
```

L'agent applique la **boucle Claude Code-like** de Deep Agents :
1. lit l'issue + clone le repo,
2. construit un plan (`write_todos`),
3. délègue à des sub-agents (lecture/codage/tests),
4. exécute, teste, itère jusqu'à passage des tests,
5. commit / push / ouvre la PR.

## Installation rapide

### 1. Pré-requis

- Python ≥ 3.11
- `git`, `gh` (GitHub CLI) authentifié
- [Ollama](https://ollama.com) avec un modèle codeur :
  ```bash
  ollama pull qwen2.5-coder:14b   # recommandé
  # ou
  ollama pull deepseek-coder-v2:16b
  ```

### 2. Installer le projet

```bash
git clone <ce-repo> gh-deepagent
cd gh-deepagent
uv venv && source .venv/bin/activate           # ou python -m venv
uv pip install -e ".[ollama]"
```

### 3. Configurer

```bash
cp .env.example .env
# édite .env :
#   GITHUB_TOKEN=ghp_xxx           (repo + pull_request scope)
#   DEEPAGENT_MODEL=ollama:qwen2.5-coder:14b
#   OLLAMA_BASE_URL=http://localhost:11434
```

### 4. Utilisation locale

```bash
# Résoudre une issue précise
gh-deepagent fix https://github.com/org/repo/issues/42

# Demande d'évolution libre
gh-deepagent evolve --repo org/repo \
  --instruction "Ajoute un endpoint /healthz qui renvoie {status:'ok'}"

# Dry-run (n'ouvre pas de PR, affiche le diff)
gh-deepagent fix <url> --dry-run
```

### 5. Activer dans GitHub Actions

Copie `.github/workflows/deepagent.yml` dans ton repo cible, ajoute le secret
`DEEPAGENT_MODEL_API_KEY` (si tu utilises un provider distant) ou laisse vide pour
Ollama auto-hébergé sur un runner self-hosted.

Puis :

- **Pose un label `deepagent`** sur n'importe quelle issue → l'agent ouvre une PR.
- **Commente `/deepagent <ta demande>`** sur une issue ou PR → l'agent exécute la
  demande d'évolution.

## Authentification GitHub

| Mode             | Quand l'utiliser                                  | Setup                                                  |
|------------------|---------------------------------------------------|--------------------------------------------------------|
| **PAT**          | CLI locale, un seul repo                          | `GITHUB_TOKEN=ghp_...`                                 |
| **GitHub App**   | Serveur webhook multi-tenant, prod                | `DEEPAGENT_GITHUB_APP_ID` + `_PRIVATE_KEY_PATH`        |

Les tokens d'installation de l'App sont **émis à la volée, valides 1h, cachés
en mémoire et auto-refreshés**. La payload webhook fournit déjà `installation.id`,
donc *zéro appel API* en plus pour résoudre les credentials.

```bash
gh-deepagent app-info     # diagnostique : liste les installations
```

Détails dans [`docs/GITHUB_APP.md`](docs/GITHUB_APP.md).

## Stack technique

- **deepagents** (LangChain) — harness + planning + filesystem + subagents
- **LangGraph** — runtime, persistence, checkpointing
- **PyGithub** — API GitHub (issues, PRs, comments)
- **GitPython** — clone, branch, commit, push
- **LiteLLM / langchain-ollama** — couche LLM
- **Typer** — CLI
- **pytest** — tests de l'agent lui-même

Voir :
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — internes, cycle de vie, sécurité.
- [`docs/CUSTOMIZATION.md`](docs/CUSTOMIZATION.md) — ajouter tools, sub-agents, skills.
- [`docs/WEBHOOK.md`](docs/WEBHOOK.md) — déployer le serveur FastAPI (Docker/systemd).
- [`docs/SANDBOXES.md`](docs/SANDBOXES.md) — basculer sur Daytona / Modal / Runloop.
- [`docs/GITHUB_APP.md`](docs/GITHUB_APP.md) — auth GitHub App (multi-tenant, prod).
- [`docs/QUEUE_AND_OBSERVABILITY.md`](docs/QUEUE_AND_OBSERVABILITY.md) — Redis, workers, Prometheus, Grafana, alertes.
- [`docs/PRODUCTION.md`](docs/PRODUCTION.md) — coût LLM, OTel, Alertmanager, quotas, SSE streaming.
- [`docs/DASHBOARD.md`](docs/DASHBOARD.md) — dashboard Streamlit d'admin (jobs, DLQ, quotas, coût, live tail).
- [`docs/MULTI_TENANT.md`](docs/MULTI_TENANT.md) — OAuth GitHub Device Flow + scoping multi-tenant des endpoints.
- [`docs/SAAS_FEATURES.md`](docs/SAAS_FEATURES.md) — coût par installation, rôles (viewer/operator/admin), audit log.
- [`docs/DEPLOY_HOSTED.md`](docs/DEPLOY_HOSTED.md) — déploiement auto sur Hugging Face Spaces et Streamlit Community Cloud à chaque push.
- [`docs/CICD.md`](docs/CICD.md) — référence des 10 workflows GH Actions (CI, release-please, GHCR, PR preview, rollback).

## Itération sur une PR existante

```bash
# CLI :
gh-deepagent iterate --repo org/repo --pr 123 \
  --instruction "Renomme la classe Foo en Bar et adapte les tests"

# Sur GitHub : commente directement sur la PR :
/deepagent ajoute un test pour le cas timeout
```

L'agent checkout la branche de la PR, applique les changements, relance les tests,
push sur la même branche et poste un commentaire récap.

## Webhook (alternative à GitHub Actions)

```bash
docker compose -f deploy/docker-compose.yml up -d --build
# expose https://deepagent.example.com/webhook via Caddy → configure ta GitHub App
```

Détails dans [`docs/WEBHOOK.md`](docs/WEBHOOK.md).

## Sandbox d'exécution

Par défaut le code tourne sur ton host. Pour isoler :

```bash
export DEEPAGENT_BACKEND=daytona   # ou modal / runloop
export DAYTONA_API_KEY=...
gh-deepagent fix <url>
```

Détails dans [`docs/SANDBOXES.md`](docs/SANDBOXES.md).
