# Sandboxes distants (Daytona / Modal / Runloop)

Pour les **repos non-confiance** ou un agent partagé multi-utilisateurs, exécute
le code dans un sandbox isolé plutôt que sur le host.

Bascule en une variable d'env :

```bash
export DEEPAGENT_BACKEND=daytona      # ou modal | runloop | local
```

Le clone Git est fait côté host (avec `GITHUB_TOKEN`), uploadé dans le sandbox
au démarrage, et **les fichiers modifiés sont re-synchronisés vers le host** au
moment de `summarize_diff` et `finalize_patch` pour que le commit + push se
fasse côté host (le sandbox n'a jamais accès au token GitHub).

```
┌─────────────────────────────────────────────────────────────────┐
│ Host (gh-deepagent)                                             │
│  ┌─────────────┐                                                │
│  │ Git clone   │──upload──▶  ┌──────────────────────────────┐   │
│  └─────────────┘             │ Daytona/Modal/Runloop sandbox│   │
│                              │  ┌─────────────┐             │   │
│                              │  │ Agent runs: │             │   │
│                              │  │ edit files  │             │   │
│                              │  │ run tests   │             │   │
│                              │  └─────────────┘             │   │
│                              └──────────────────────────────┘   │
│  ┌─────────────┐  ◀──download──                                 │
│  │ git commit  │                                                │
│  │ git push    │                                                │
│  │ open PR     │                                                │
│  └─────────────┘                                                │
└─────────────────────────────────────────────────────────────────┘
```

## Daytona

```bash
pip install -e ".[daytona]"
export DAYTONA_API_KEY=...
export DAYTONA_API_URL=https://app.daytona.io/api
export DEEPAGENT_BACKEND=daytona
gh-deepagent fix https://github.com/org/repo/issues/42
```

Auto-stop : configuré à 15 min d'idle par défaut côté Daytona (modifiable dans
`backends/__init__.py`).

## Modal

```bash
pip install -e ".[modal]"
modal token new                         # ou exporte MODAL_TOKEN_ID/MODAL_TOKEN_SECRET
export DEEPAGENT_BACKEND=modal
export DEEPAGENT_MODAL_APP=gh-deepagent
export DEEPAGENT_MODAL_IMAGE=python:3.12-slim
gh-deepagent fix <url>
```

Le sandbox est terminé automatiquement par `handle.cleanup()` en fin de run.

## Runloop

```bash
pip install -e ".[runloop]"
export RUNLOOP_API_KEY=...
export DEEPAGENT_BACKEND=runloop
gh-deepagent fix <url>
```

## Choisir le bon backend

| Contexte                                          | Backend    |
|---------------------------------------------------|------------|
| Tu codes en local, repos perso, vitesse max       | `local`    |
| GitHub Actions, repos internes confiance          | `local`    |
| Repos open-source / multi-tenant / SaaS           | `daytona`  |
| Besoin GPU / images Docker custom                 | `modal`    |
| Stateful devbox réutilisable entre runs           | `runloop`  |

## Performance

L'upload/download initial est en `O(taille du repo)`. Pour des monorepos lourds,
ajoute un `.gitignore`-like filter dans `backends/__init__.py:_walk_files()`.
