# Webhook server (alternative à GitHub Actions)

Si tu ne veux pas embarquer un workflow CI dans chaque repo cible, tu peux héberger
un **serveur webhook FastAPI** qui réagit aux événements GitHub via une *GitHub App*
ou un simple *repository webhook*.

## Pourquoi un webhook plutôt que Actions ?

| Critère                        | GitHub Actions          | Webhook server             |
|--------------------------------|-------------------------|----------------------------|
| Setup par repo                 | un fichier `.yml`       | une URL à coller           |
| GPU local pour Ollama          | runner self-hosted      | n'importe quel hôte        |
| Concurrence / file d'attente   | concurrency group       | thread pool partagé        |
| Logs centralisés               | par run, par repo       | un seul service            |
| Coût                           | runner minutes          | un VPS                     |
| Latence de démarrage           | ~30-60 s (cold runner)  | ~1 s                       |

## Architecture

```
GitHub  ──HTTPS──▶  Caddy (TLS) ──▶  uvicorn / FastAPI  ──▶  ThreadPool
                                            │                      │
                                            ▼                      ▼
                                      /healthz             _dispatch(event)
                                      /webhook              ├── fix_issue
                                                            ├── evolve_code
                                                            ├── iterate_pr
                                                            └── review_pr
```

- **Signature** : chaque payload est validé via HMAC-SHA256 (`X-Hub-Signature-256`)
  contre `DEEPAGENT_WEBHOOK_SECRET`.
- **Dédup** : `X-GitHub-Delivery` est mémorisé 10 min pour absorber les retries.
- **Réponse rapide** : on répond `202 accepted` en <50 ms et on lance le travail
  dans un `ThreadPoolExecutor`, pour rester sous le timeout de 10 s de GitHub.

## Déploiement Docker Compose

```bash
cd gh-deepagent
cp .env.example .env       # remplis GITHUB_TOKEN, DEEPAGENT_WEBHOOK_SECRET, ...
docker compose -f deploy/docker-compose.yml up -d --build
# pull du modèle Ollama
docker exec -it ollama ollama pull qwen2.5-coder:14b
```

Le service expose :
- `GET  http://<host>:8080/healthz`
- `POST http://<host>:8080/webhook`

Devant, **Caddy** s'occupe du TLS automatique (Let's Encrypt) sur ton domaine
(voir `deploy/Caddyfile`).

## Configurer la GitHub App

1. https://github.com/settings/apps → **New GitHub App**
2. Webhook URL : `https://deepagent.example.com/webhook`
3. Webhook secret : valeur de `DEEPAGENT_WEBHOOK_SECRET`
4. Permissions :
   - Repository → Contents : **Read & write**
   - Repository → Issues : **Read & write**
   - Repository → Pull requests : **Read & write**
   - Repository → Metadata : **Read**
5. Subscribe to events : *Issues*, *Issue comment*, *Pull request*
6. Installe l'App sur ton org / tes repos.

> Note : pour la prod, remplace `GITHUB_TOKEN` (PAT) par l'authentification
> *GitHub App* (génération JWT + token d'installation). Voir
> [`PyGithub` doc](https://pygithub.readthedocs.io/en/latest/examples/Authentication.html#app-authentication).

## Déploiement systemd (sans Docker)

```bash
sudo useradd -r -m -d /opt/gh-deepagent deepagent
sudo -u deepagent git clone https://github.com/you/gh-deepagent /opt/gh-deepagent
cd /opt/gh-deepagent
sudo -u deepagent python -m venv .venv
sudo -u deepagent .venv/bin/pip install -e ".[webhook,ollama]"
sudo cp .env.example /etc/gh-deepagent.env  # à compléter
sudo cp deploy/systemd-gh-deepagent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gh-deepagent
```

## Sécurité

- Le secret HMAC est **obligatoire** en prod (sinon le serveur skip la vérif et
  loggue un warning — pratique en dev uniquement).
- Tourne le service avec un user dédié et `DEEPAGENT_BACKEND=daytona` (ou modal)
  pour que l'exécution de code se fasse dans un sandbox jetable, pas sur le host.
- Mets une CAP : `DEEPAGENT_MAX_TURNS=40` pour borner le coût.
- Backup le volume `workdir` si tu tiens à un cache des clones (sinon il est
  jetable).
