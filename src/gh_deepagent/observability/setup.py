"""One-call setup for the whole observability stack."""
from __future__ import annotations

import logging
import os

from .logging_setup import setup_logging

log = logging.getLogger(__name__)


def setup_observability(service_name: str = "gh-deepagent") -> None:
    """Initialise logs + metrics + (optionally) LangSmith + OpenTelemetry tracing.

    Called once at process start (webhook server, worker, CLI command).
    Safe to call multiple times.
    """
    setup_logging(level=os.getenv("DEEPAGENT_LOG_LEVEL", "INFO"))

    # LangSmith — opt-in via standard env vars (LangChain reads them itself).
    if os.getenv("LANGSMITH_TRACING", "").lower() in ("1", "true", "yes"):
        os.environ.setdefault("LANGSMITH_PROJECT", "gh-deepagent")
        log.info("LangSmith tracing enabled (project=%s)", os.environ["LANGSMITH_PROJECT"])

    # OpenTelemetry — opt-in via DEEPAGENT_OTEL_ENABLED.
    from .tracing import setup_tracing
    setup_tracing(service_name=service_name)
