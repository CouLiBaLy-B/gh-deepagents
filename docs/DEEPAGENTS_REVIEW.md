# Revue `deepagents` — best practices, comparaison & plan d'amélioration

> Date : 2026-06-20 · Cible : `gh-deepagent` v0.6.1 · Lib : `deepagents` (pin `>=0.6.3,<0.8.0`, dernière 0.6.x = **0.6.11**)

Ce document fait (1) la synthèse de l'état de l'art `deepagents`, (2) la comparaison
avec la solution actuelle, et (3) un plan d'amélioration priorisé et chiffré.

---

## 1. Méthodologie & sources

- Lecture exhaustive du code : `agent.py`, `tools.py`, `runner.py`, `models.py`,
  `prompts.py`, `backends/__init__.py`, `config.py`, `observability/middleware.py`,
  `AGENTS.md`, `.deepagents/skills/*`.
- Documentation officielle LangChain Deep Agents (overview, skills, subagents,
  human-in-the-loop, middleware) + référence API + DeepWiki.

Sources clés :
- [Deep Agents — overview](https://docs.langchain.com/oss/python/deepagents/overview)
- [Deep Agents — Skills](https://docs.langchain.com/oss/python/deepagents/skills)
- [Deep Agents — Subagents](https://docs.langchain.com/oss/python/deepagents/subagents)
- [Deep Agents — Human-in-the-loop](https://docs.langchain.com/oss/python/deepagents/human-in-the-loop)
- [Référence API deepagents](https://reference.langchain.com/python/deepagents)
- [GitHub langchain-ai/deepagents](https://github.com/langchain-ai/deepagents)
- [DeepWiki — Skills / Middleware / Sub-agent workflows](https://deepwiki.com/langchain-ai/deepagents)

---

## 2. État de l'art `deepagents` (ce que la lib fournit nativement)

`deepagents` est un *harness* opinioné au-dessus de `create_agent` (LangGraph). Il
livre par défaut :

**Stack middleware par défaut** : `todos` (planning), `memory` (AGENTS.md), `skills`,
`filesystem`, `subagents`, `summarization` (compaction auto du contexte), prompt
caching (Anthropic).

**Tools intégrés** : `ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`,
`execute`, `write_todos`, `task` (délégation sous-agent), `compact_conversation`.

**Paramètres `create_deep_agent`** :
`model`, `tools`, `system_prompt`, `subagents`, `middleware`, `backend`, `store`,
**`skills`**, **`memory`**, **`permissions`**, **`interrupt_on`**, `excluded_tools`,
`excluded_middleware`, `checkpointer`, `response_format`.

**Capacités notables** :
- **Skills** (progressive disclosure) : dossiers `SKILL.md` (frontmatter `name` +
  `description`) chargés à la demande. Niveau 1 = nom+description en system prompt ;
  niveau 2 = corps du SKILL.md au moment où il devient pertinent ; niveau 3 =
  `scripts/`, `references/`, `assets/` à la demande.
- **Sous-agents hétérogènes** : chaque sous-agent peut avoir son **propre `model`**,
  ses `tools`, son `system_prompt`, son `middleware`, ses `skills`, son
  `interrupt_on`, ses `permissions`, son `response_format`.
- **HITL** via `interrupt_on={tool: True|False|{"allowed_decisions":[...]}}` +
  `checkpointer` (décisions `approve`/`edit`/`reject`/`respond`).
- **Permissions filesystem** déclaratives par globs (allow/deny).
- **Backends** : `LocalShellBackend`, `StoreBackend`, `CompositeBackend`, sandboxes
  Daytona/Modal/Runloop.

---

## 3. Forces de la solution actuelle

La base est **solide et au-dessus de la moyenne** pour un wrapper deepagents :

- ✅ **Équipe de 11 sous-agents** spécialisés avec **least-privilege** réel
  (`Toolbox.for_role`) — excellent design de sécurité.
- ✅ **Tools métier GitHub** bien pensés (`fetch_issue`, `finalize_patch` idempotent
  avec garde anti-`main`, mode *iterate-on-PR*).
- ✅ **Sortie structurée** du reviewer via Pydantic (`ReviewReport` + `response_format`).
- ✅ **Observabilité de prod** maison : Prometheus + OpenTelemetry via
  `MetricsMiddleware` (le bon point d'extension), cost callback par token, Grafana/Tempo.
- ✅ **Multi-backend** (local + 3 sandboxes) + **mémoire en couches** optionnelle
  (`CompositeBackend` + `StoreBackend` sous `/memories/<repo>/`).
- ✅ **Multi-provider** LLM (ollama / openrouter / anthropic / openai / google / groq).
- ✅ Dégradation gracieuse quand deepagents est ancien (try/except sur imports).

---

## 4. Écarts vs best practices (priorisé)

| # | Écart | Impact | Effort | Prio |
|---|-------|--------|--------|------|
| 1 | **Skills non branchés** : `.deepagents/skills/*.md` existent mais `create_deep_agent` est appelé **sans `skills=`**, et les fichiers ne sont pas au format `SKILL.md` (dossier + frontmatter). 9 skills = code mort. | Élevé | Faible | **P0** |
| 2 | **Pas de routage modèle par sous-agent** : les 11 sous-agents partagent `build_model()`. Or planner/reviewer/docs/i18n peuvent tourner sur un modèle bon-marché. | Élevé (coût) | Faible | **P0** |
| 3 | **Redondance de tools** : `read_file_range`, `search_code`, `list_project_files`, `git_*` doublonnent les natifs `read_file`/`grep`/`glob`/`ls`/`execute`. Deux façons de tout faire → confusion modèle + tokens. | Moyen | Moyen | **P1** |
| 4 | **`memory` natif non utilisé** : on injecte AGENTS.md/mémoire à la main dans le system prompt au lieu du paramètre `memory=`. | Moyen | Faible | **P1** |
| 5 | **`permissions` filesystem non utilisées** : aucune protection déclarative (`.git/`, lockfiles, `*_pb2.py`, `dist/`, fichiers générés, CI secrets). | Moyen (sécurité) | Faible | **P1** |
| 6 | **HITL `interrupt_on` non câblé** : documenté dans ARCHITECTURE mais jamais implémenté. Pour `finalize_patch`, `codemod_python`, `ast_grep_rewrite(apply=True)` en CLI local, c'est un garde-fou clé. | Moyen | Faible | **P1** |
| 7 | **`codemod_python` fait `exec()` in-process** : exécute du code LLM dans le process hôte (les remote sandboxes ne le protègent pas car ce tool tourne côté hôte). Anti-pattern relevé par votre propre `security` prompt. | Élevé (sécurité) | Moyen | **P1** |
| 8 | **Pas de tracing LangSmith** : best-practice #1 des docs. Le Prometheus/OTel maison couvre l'ops, pas le *debug/eval au niveau agent* (replay de traces, eval de prompts). | Moyen | Faible | **P2** |
| 9 | **Aucune éval de l'agent** : tests unitaires OK, mais pas de boucle d'évaluation des sorties agent (jeux d'issues de référence, scoring). | Moyen | Élevé | **P2** |
| 10 | **Skills des sous-agents non hérités** : les custom subagents n'héritent pas auto des skills du lead — il faut leur passer `skills=` explicitement. | Faible | Faible | **P2** |
| 11 | **Streaming ad-hoc** : itération brute des chunks dans `_stream` au lieu des projections d'événements typées. Fragile aux changements de schéma deepagents. | Faible | Moyen | **P2** |
| 12 | **Pin large `<0.8.0`** : les API skills/memory/permissions/interrupt_on sont stabilisées en 0.6.x ; vaut le coup de fixer un plancher plus haut et tester 0.8 dans une CI dédiée. | Faible | Faible | **P2** |

---

## 5. Plan d'amélioration

### Phase 0 — Quick wins coût/valeur (½–1 j) — **P0**

**5.1 Brancher les Skills (#1).**
1. Migrer `.deepagents/skills/<x>.md` → `.deepagents/skills/<x>/SKILL.md` avec
   frontmatter :
   ```markdown
   ---
   name: python-conventions
   description: Conventions Python (ruff, typing, imports, erreurs, async). À activer quand le repo cible est Python.
   ---
   ```
2. Passer le dossier au constructeur dans `agent.py` :
   ```python
   from pathlib import Path
   SKILLS_DIR = Path(__file__).resolve().parents[2] / ".deepagents" / "skills"
   agent = create_deep_agent(
       ...,
       skills=[str(SKILLS_DIR)] if SKILLS_DIR.exists() else [],
   )
   ```
   ⚠️ Avec un backend non-local (sandbox / StoreBackend), fournir le contenu des
   skills via `files=` à l'`invoke`/`stream` (cf. doc Skills).

**5.2 Routage modèle par sous-agent (#2).**
Ajouter `DEEPAGENT_MODEL_CHEAP` (ex. `openrouter:...haiku`/`gpt-…-mini`) et l'affecter
aux rôles lecture/rédaction légère :
```python
cheap = build_model(os.getenv("DEEPAGENT_MODEL_CHEAP")) if os.getenv("DEEPAGENT_MODEL_CHEAP") else model
CHEAP_ROLES = {"planner", "reviewer", "docs-writer", "i18n", "security"}
# dans la construction de chaque subagent :
"model": cheap if name in CHEAP_ROLES else model,
```
Gain typique : **−30 à −60 % de coût** sans perte de qualité sur le chemin critique
(coder/debugger gardent le gros modèle).

### Phase 1 — Robustesse & sécurité (2–3 j) — **P1**

**5.3 Dégraisser les tools redondants (#3).** S'appuyer sur les natifs
`read_file`/`grep`/`glob`/`ls`/`execute` ; ne conserver en custom que la **valeur
ajoutée** (`fetch_issue`, `summarize_diff`, `analyze_test_failure`, `finalize_patch`,
`run_tests`, audits, migrate/perf/i18n). Masquer les natifs non voulus via
`excluded_tools=[...]`. Objectif : un seul chemin par capacité.

**5.4 `memory` natif (#4).** Remplacer l'injection manuelle par
`create_deep_agent(..., memory=[".deepagents/AGENTS.md", <memory_path>])` (selon
signature installée) et garder la mémoire en couches (`CompositeBackend`) pour la
persistance inter-jobs.

**5.5 Permissions filesystem (#5).**
```python
permissions=[
    {"deny": [".git/**", "**/*_pb2.py", "**/*.generated.*", "dist/**", "build/**",
              "**/node_modules/**", ".github/workflows/**"]},
]
```
Défense en profondeur en plus de `root_dir` et du least-privilege.

**5.6 HITL `interrupt_on` (#6).** En CLI local (flag `--interactive`), exiger
l'approbation des tools destructeurs :
```python
interrupt_on={
    "finalize_patch": True,
    "ast_grep_rewrite": {"allowed_decisions": ["approve", "edit", "reject"]},
    "codemod_python": True,
}  # + checkpointer=MemorySaver()
```
En mode CI/non-interactif : laisser `{}` (autonome).

**5.7 Sécuriser `codemod_python` (#7).** Ne plus `exec()` dans le process hôte :
- soit exécuter le script via `backend.execute(...)` (donc dans le sandbox quand actif) ;
- soit `subprocess` Python isolé avec timeout + cwd=repo, sans accès réseau ;
- a minima : restreindre les builtins et interdire `import os/subprocess/socket`.

### Phase 2 — Qualité & durabilité (3–5 j) — **P2**

- **5.8 LangSmith (#8)** : activer le tracing (`LANGSMITH_TRACING=true`,
  `LANGSMITH_API_KEY`) en complément du stack Prometheus/OTel. Replays + eval de prompts.
- **5.9 Eval agent (#9)** : jeu d'issues de référence (repo fixtures) + scoring
  (tests passent ? PR ouverte ? diff minimal ?) lancé en CI nightly.
- **5.10 Skills par sous-agent (#10)** : ex. `migrator` reçoit `migration-playbook`,
  `perf-analyst` reçoit `perf-playbook`, etc. via `"skills": [...]` dans chaque spec.
- **5.11 Streaming typé (#11)** : remplacer l'itération brute par les projections
  d'événements officielles si disponibles dans la version installée.
- **5.12 Dépendances (#12)** : remonter le plancher `deepagents>=0.6.11` et ajouter
  une CI matrice `0.6.x` / `0.8.x` pour anticiper la migration.

---

## 6. Récapitulatif "à faire en premier"

1. **Brancher les skills** (`skills=`) + format `SKILL.md` — *9 skills actuellement morts*.
2. **Modèle bon-marché par sous-agent** — *gain coût immédiat*.
3. **Sécuriser `codemod_python`** — *exécution de code LLM en clair*.
4. **`interrupt_on` + permissions** — *garde-fous destructeurs*.
5. **Dégraisser les tools doublons** — *moins de confusion, moins de tokens*.

Ces cinq points couvrent ~80 % de la valeur pour ~30 % de l'effort total.
