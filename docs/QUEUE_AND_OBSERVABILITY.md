# Queue & Observability

## Pourquoi

Avant : le webhook server traitait les jobs **dans le même process** (ThreadPoolExecutor).
Conséquences : crash = job perdu, scaling = vertical seulement, pas d'isolation
entre repos, observabilité = `print()`.

Maintenant : architecture **webhook → Redis → workers**, avec métriques
Prometheus, logs structurés JSON, traces LangSmith et dashboard Grafana
provisionné.

```
                       ┌──────────────────┐
GitHub ──https──▶      │  FastAPI         │
                       │  /webhook        │──▶ verify HMAC
                       │  /healthz        │    dedup (Redis SETNX)
                       │  /metrics        │    enqueue (LPUSH job)
                       │  /jobs/{id}      │
                       │  /dlq            │◀── 202 accepted
                       └────────┬─────────┘
                                │
                       ┌────────▼─────────┐
                       │  Redis           │  queue, dedup, locks, status, logs
                       └────┬──────┬──────┘
                            │      │
              ┌─────────────┘      └─────────────┐
              │                                  │
       ┌──────▼──────┐                    ┌──────▼──────┐
       │  Worker #1  │  BRPOPLPUSH        │  Worker #N  │
       │             │  acquire repo lock │             │
       │  agent.run  │  dispatch          │  agent.run  │
       │             │  metrics/logs      │             │
       └─────────────┘                    └─────────────┘

                       ┌──────────────────┐
                       │  Prometheus      │ scrape /metrics
                       │  Grafana         │ dashboard "gh-deepagent"
                       │  LangSmith       │ agent traces (opt-in)
                       └──────────────────┘
```

## Garanties

| Propriété                          | Implémentation                                                   |
|------------------------------------|------------------------------------------------------------------|
| **At-least-once**                  | `BRPOPLPUSH` vers la processing-list par worker                  |
| **Crash recovery**                 | `recover_orphans()` au démarrage du worker                       |
| **Idempotence webhook**            | `SET NX EX` sur `dedup:<delivery_id>` (TTL 10 min)               |
| **Pas de concurrence sur un repo** | `SET NX EX` sur `repo_lock:<repo>` (TTL 30 min)                  |
| **Retries avec backoff**           | 2s, 8s, 32s — max `DEEPAGENT_MAX_ATTEMPTS`                       |
| **Dead-letter queue**              | `LPUSH deepagent:queue:dead` après épuisement des tentatives     |
| **Scale horizontal**               | N workers indépendants, pas de coordination autre que Redis      |
| **Backpressure**                   | Webhook répond 202 immédiat ; la file absorbe les pics           |

## Lancement complet (Docker)

```bash
cd gh-deepagent/deploy
cp ../.env.example ../.env       # remplis les secrets
docker compose up -d --build

# pull le modèle (une fois)
docker exec -it ollama ollama pull qwen2.5-coder:14b

# scale les workers
docker compose up -d --scale gh-deepagent-worker=4
```

Endpoints exposés :

| URL                           | Quoi                                       |
|-------------------------------|--------------------------------------------|
| `http://host:8080/webhook`    | GitHub envoie ici (configure ta GitHub App)|
| `http://host:8080/healthz`    | Liveness + Redis + queue depth             |
| `http://host:8080/metrics`    | Prometheus exporter                        |
| `http://host:8080/jobs/{id}`  | Statut d'un job (debug)                    |
| `http://host:8080/dlq`        | Contenu du dead-letter                     |
| `http://host:9090`            | Prometheus UI                              |
| `http://host:3000`            | Grafana (dashboard *gh-deepagent*)         |

## CLI

```bash
# Worker (en local pour debug)
gh-deepagent worker --workers 2

# Inspection
gh-deepagent queue stats
gh-deepagent queue dlq
gh-deepagent queue show <job-id>
gh-deepagent queue requeue <job-id>
```

## Métriques exposées

| Nom                                            | Type      | Labels                | À quoi ça sert                          |
|------------------------------------------------|-----------|-----------------------|------------------------------------------|
| `deepagent_jobs_total`                         | counter   | event, status         | Throughput, taux de succès               |
| `deepagent_job_duration_seconds`               | histogram | event, status         | Latence p50/p95/p99                      |
| `deepagent_queue_depth`                        | gauge     | —                     | Backlog ; alerte si > N                  |
| `deepagent_dlq_size`                           | gauge     | —                     | Alerte critique si > 0                   |
| `deepagent_jobs_in_progress`                   | gauge     | worker                | Utilisation des workers                  |
| `deepagent_tool_calls_total`                   | counter   | tool, status          | Top tools / taux d'erreur par tool       |
| `deepagent_tool_duration_seconds`              | histogram | tool                  | Tools lents                              |
| `deepagent_subagent_calls_total`               | counter   | subagent              | Quels sub-agents le lead délègue le plus |
| `deepagent_llm_tokens_total`                   | counter   | provider, model, kind | Coût $$ (à brancher avec un callback LangChain) |

## Alertes recommandées (Prometheus)

```yaml
groups:
- name: gh-deepagent
  rules:
  - alert: DeepagentDLQNotEmpty
    expr: deepagent_dlq_size > 0
    for: 5m
    labels: { severity: warning }
    annotations: { summary: "{{ $value }} jobs in DLQ — needs human review" }

  - alert: DeepagentQueueBacklog
    expr: deepagent_queue_depth > 20
    for: 10m
    labels: { severity: warning }
    annotations: { summary: "Queue backlog growing — add workers" }

  - alert: DeepagentJobFailureRate
    expr: |
      sum(rate(deepagent_jobs_total{status="failed"}[15m])) /
      sum(rate(deepagent_jobs_total[15m])) > 0.20
    for: 15m
    labels: { severity: page }
    annotations: { summary: "> 20% jobs failing" }

  - alert: DeepagentNoWorkers
    expr: sum(deepagent_jobs_in_progress) == 0 and deepagent_queue_depth > 0
    for: 5m
    labels: { severity: page }
    annotations: { summary: "Queue has work but nothing is running" }
```

## Logs structurés

Tout passe par `structlog` → JSON sur stdout (un objet par ligne). Champs
toujours présents : `timestamp`, `level`, `event`. Champs contextuels bindés
automatiquement par `bind(job_id=..., repo=..., gh_event=...)` au démarrage
d'un job.

Exemple :
```json
{"timestamp": "2026-06-03T12:34:56Z", "level": "info", "event": "job enqueued",
 "job_id": "abc-123", "repo": "org/api", "gh_event": "issues"}
```

Pour le dev :
```bash
DEEPAGENT_JSON_LOGS=0 gh-deepagent worker      # logs colorés / lisibles
```

## Tracing (LangSmith)

```bash
export LANGSMITH_TRACING=true
export LANGSMITH_API_KEY=lsv2_pt_...
export LANGSMITH_PROJECT=gh-deepagent
```

Chaque exécution d'agent (tool calls, sub-agent delegations, tokens LLM)
apparaît dans LangSmith comme une trace hiérarchique. Indispensable pour
debugger un échec en prod sans reproduire localement.

## Dimensionnement

Règles de pouce :
- 1 worker peut traiter ~1 job en parallèle (l'agent est CPU+IO mixte)
- Un job typique fait 30 s – 5 min selon la taille de l'issue et du repo
- Pour ~100 issues/heure : prévoir ~6 workers
- Redis : 256 Mo suffit largement (configuré dans `docker-compose.yml`)
- Pas de besoin de cluster Redis : single-node + AOF persistence est ok

## Migration depuis la v1 (ThreadPoolExecutor)

L'API publique du webhook est inchangée — seule la *forme* du JSON de
réponse change :

- avant : `{"accepted": true, "event": "issues"}`
- après : `{"accepted": true, "job_id": "<uuid>", "event": "issues"}`

Tu peux suivre un job précis via `GET /jobs/{id}`.
