"""Prometheus metrics + ASGI app exposing /metrics."""
from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

# Use a single global registry so the metrics module is import-safe everywhere.
REGISTRY = CollectorRegistry()

# ---------------------------------------------------------------- jobs
JOBS_TOTAL = Counter(
    "deepagent_jobs_total",
    "Total jobs by event and final status.",
    labelnames=("event", "status"),
)
JOB_DURATION = Histogram(
    "deepagent_job_duration_seconds",
    "Wall-clock duration of a job.",
    labelnames=("event", "status"),
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800),
)
IN_PROGRESS = Gauge(
    "deepagent_jobs_in_progress",
    "Currently running jobs per worker.",
    labelnames=("worker",),
)
QUEUE_DEPTH = Gauge(
    "deepagent_queue_depth",
    "Pending jobs in the main queue.",
)
DLQ_SIZE = Gauge(
    "deepagent_dlq_size",
    "Number of dead-letter jobs awaiting human review.",
)

# ---------------------------------------------------------------- agent internals
TOOL_CALLS = Counter(
    "deepagent_tool_calls_total",
    "Tool invocations by name and outcome.",
    labelnames=("tool", "status"),
)
TOOL_DURATION = Histogram(
    "deepagent_tool_duration_seconds",
    "Tool wall-clock duration.",
    labelnames=("tool",),
    buckets=(0.05, 0.25, 1, 5, 15, 60, 300, 900),
)
SUBAGENT_CALLS = Counter(
    "deepagent_subagent_calls_total",
    "How often each sub-agent was invoked.",
    labelnames=("subagent",),
)
LLM_TOKENS = Counter(
    "deepagent_llm_tokens_total",
    "LLM tokens consumed.",
    labelnames=("provider", "model", "kind"),     # kind = input / output
)
LLM_CALLS = Counter(
    "deepagent_llm_calls_total",
    "Number of LLM calls.",
    labelnames=("provider", "model"),
)
LLM_COST_USD = Counter(
    "deepagent_llm_cost_usd_total",
    "Estimated LLM spend (USD).",
    labelnames=("provider", "model"),
)

# ---------------------------------------------------------------- quotas / rate limiting
QUOTA_REJECTIONS = Counter(
    "deepagent_quota_rejections_total",
    "Webhook requests rejected because the installation's quota was exceeded.",
    labelnames=("installation_id",),
)


# ---------------------------------------------------------------- /metrics ASGI app
async def metrics_app(scope, receive, send):
    """Minimal ASGI endpoint that serves Prometheus metrics on /metrics.

    Mounted by the webhook server but works standalone too.
    """
    if scope["type"] != "http":
        return
    payload = generate_latest()
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", CONTENT_TYPE_LATEST.encode())],
    })
    await send({"type": "http.response.body", "body": payload})


# ---------------------------------------------------------------- decorator helpers

def track_tool(name: str):
    """Decorator: count calls + record duration for a tool function."""
    import functools, time as _time

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            t0 = _time.perf_counter()
            try:
                out = fn(*a, **kw)
                TOOL_CALLS.labels(name, "ok").inc()
                return out
            except Exception:
                TOOL_CALLS.labels(name, "error").inc()
                raise
            finally:
                TOOL_DURATION.labels(name).observe(_time.perf_counter() - t0)
        return wrapper
    return deco
