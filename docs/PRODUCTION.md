# Production checklist & advanced features

Everything in this doc is **opt-in** via env vars. The system runs perfectly
without it; activate piece by piece as you grow.

## 1. LLM cost tracking

Automatically counts tokens and prices every LLM call.

```env
# Optional: override prices (USD per 1M tokens) for fine-tuned / custom models.
DEEPAGENT_PRICE_OVERRIDES={"openai:my-finetune-001":{"input":1.5,"output":6.0}}
```

Built-in catalog covers the main hosted providers; local models (Ollama, vLLM)
are priced at $0 but tokens are still counted.

New metrics:
- `deepagent_llm_tokens_total{provider,model,kind=input|output}`
- `deepagent_llm_calls_total{provider,model}`
- `deepagent_llm_cost_usd_total{provider,model}`

Grafana panels added: spend last 1h / 24h, spend per model ($/hr).

Alert: `DeepagentLLMSpendBurst` fires when spend > $20 in 1h (runaway loop).

## 2. OpenTelemetry distributed tracing

```env
DEEPAGENT_OTEL_ENABLED=1
OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4317
```

Install the optional extra:

```bash
pip install -e ".[otel]"
```

Spans emitted, with parent-child propagation via the `traceparent` field
embedded in each job payload:

```
webhook.receive  (FastAPI auto-instr)
└── job.enqueue
    └── job.process  (worker; chained across processes via traceparent)
        └── agent.stream
            └── tool.<name>   (one span per tool call)
```

Logs are auto-correlated: every structlog entry inside a span gets `trace_id`
and `span_id` fields, so you can pivot from a slow trace in Tempo to the
matching JSON log lines.

Activated stack (`--profile observability`):

```bash
docker compose --profile observability -f deploy/docker-compose.yml up -d
```

Grafana auto-provisions a Tempo datasource so traces show up in the
*Explore → Tempo* panel next to your metrics.

## 3. Alertmanager

`deploy/alertmanager/alertmanager.yml` routes:

| Severity   | Receiver         |
|------------|------------------|
| `page`     | PagerDuty        |
| `warning`  | Slack `#gh-deepagent-alerts` |
| `critical` | Slack `#gh-deepagent-alerts` |
| `info`     | Slack `#gh-deepagent-info`   |

Inhibition rule: if `DeepagentWebhookDown` fires, suppress `JobFailureRateHigh`
and `ToolErrorSpike` (root cause first).

Configure secrets:

```env
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
PAGERDUTY_SERVICE_KEY=...
```

Pre-loaded rules in `deploy/prometheus/alerts.yml`:

- `DeepagentDLQNotEmpty` — warning, 5 min
- `DeepagentQueueBacklog` — warning, > 20 jobs for 10 min
- `DeepagentQueueStuck` — critical, queue has work but no worker
- `DeepagentJobFailureRateHigh` — page, > 20% failures over 15 min
- `DeepagentToolErrorSpike` — warning, any tool errors > 0.5/s
- `DeepagentJobP95SlowdownExtreme` — warning, p95 > 15 min
- `DeepagentLLMSpendBurst` — warning, > $20/h
- `DeepagentQuotaRejectionsSpike` — info, recurring 429s for one installation
- `DeepagentWebhookDown` — page, /healthz unreachable 2 min

## 4. Per-installation quotas

Three sliding-window buckets, all opt-in:

```env
DEEPAGENT_QUOTA_HOUR=50          # max accepted jobs per installation per hour
DEEPAGENT_QUOTA_DAY=300          # per day
DEEPAGENT_QUOTA_CONCURRENT=3     # max in-flight jobs per installation

# Per-installation overrides
DEEPAGENT_QUOTA_OVERRIDES={"1234567":{"hour":500,"day":3000,"concurrent":10}}
```

When a quota is exceeded the webhook returns HTTP 429 with:

```json
{
  "error": "quota_exceeded",
  "bucket": "hour",
  "current": 50,
  "limit": 50,
  "retry_after_seconds": 1842
}
```

…plus a `Retry-After` header. Metric `deepagent_quota_rejections_total{installation_id}` is incremented.

Inspect usage live:

```bash
curl http://host:8080/installations/1234567/quota | jq
```

The worker calls `release_concurrent()` on every job completion (success,
failure or DLQ) so the concurrent counter can never get stuck.

## 5. SSE live log streaming

```bash
# CURL
curl -N http://host:8080/jobs/<job-id>/stream

# Browser
const es = new EventSource("/jobs/<job-id>/stream");
es.addEventListener("log",    e => console.log(e.data));
es.addEventListener("status", e => {
    const s = JSON.parse(e.data);
    if (["succeeded","failed","dead"].includes(s.status)) es.close();
});
```

Events:
- `log` — one line of agent output (sub-agent name + role + content preview)
- `status` — JSON `{status, result, error}` on every transition

Implementation:
- `JobQueue.append_log()` writes to a capped list **and** `PUBLISH`es on the
  per-job channel `deepagent:stream:<job-id>`
- `JobQueue.publish_status()` is called on every `update(status=...)`
- The webhook's `/jobs/{id}/stream` endpoint replays the existing log buffer
  first, then subscribes to the channel; closes the connection once the job
  reaches a terminal state
- `Cache-Control: no-cache` + `X-Accel-Buffering: no` prevents nginx buffering

The agent's `_stream()` automatically detects when it's running inside a job
(via the structlog context's `job_id`) and pushes every step to Redis pub/sub.

## Putting it all together

```bash
# Full stack with everything enabled
cd gh-deepagent
cp .env.example .env
# Edit .env: GITHUB_TOKEN / App credentials, SLACK_WEBHOOK_URL, quotas, OTEL_ENABLED=1

docker compose --profile observability -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml up -d --scale gh-deepagent-worker=4
```

URLs:

| URL                                  | What                                         |
|--------------------------------------|----------------------------------------------|
| `http://host:8080/webhook`           | GitHub posts here                            |
| `http://host:8080/jobs/{id}/stream`  | SSE live tail (use in browser/curl -N)       |
| `http://host:8080/installations/{id}/quota` | Quota inspection                       |
| `http://host:3000`                   | Grafana (dashboard + traces via Tempo)       |
| `http://host:9090`                   | Prometheus + rules tab                       |
| `http://host:9093`                   | Alertmanager UI (silences, route tree)       |
| `http://host:3200`                   | Tempo HTTP (only useful for direct queries)  |
