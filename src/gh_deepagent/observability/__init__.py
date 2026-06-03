"""Observability: structured logs (structlog), metrics (Prometheus), tracing (LangSmith).

Initialise everything at process startup via `setup_observability()`.
"""
from .logging_setup import bind, setup_logging, unbind
from .metrics import (
    DLQ_SIZE,
    IN_PROGRESS,
    JOBS_TOTAL,
    JOB_DURATION,
    LLM_CALLS,
    LLM_COST_USD,
    LLM_TOKENS,
    QUEUE_DEPTH,
    QUOTA_REJECTIONS,
    SUBAGENT_CALLS,
    TOOL_CALLS,
    TOOL_DURATION,
    metrics_app,
)
from .setup import setup_observability

__all__ = [
    "setup_observability", "setup_logging", "bind", "unbind", "metrics_app",
    "JOBS_TOTAL", "JOB_DURATION", "QUEUE_DEPTH", "DLQ_SIZE", "IN_PROGRESS",
    "TOOL_CALLS", "TOOL_DURATION", "SUBAGENT_CALLS",
    "LLM_TOKENS", "LLM_CALLS", "LLM_COST_USD", "QUOTA_REJECTIONS",
]
