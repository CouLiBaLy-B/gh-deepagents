"""Structured logging via structlog → JSON to stdout (or pretty in dev)."""
from __future__ import annotations

import logging
import os
import sys

import structlog

_initialised = False


def setup_logging(level: str = "INFO", json_logs: bool | None = None) -> None:
    """Configure structlog + stdlib logging once per process."""
    global _initialised
    if _initialised:
        return
    _initialised = True

    if json_logs is None:
        json_logs = os.getenv("DEEPAGENT_JSON_LOGS", "1") not in ("0", "false", "no", "")

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        timestamper,
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]
    if json_logs:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Pipe stdlib loggers (PyGithub, langchain, ...) through structlog too.
    logging.basicConfig(
        level=level.upper(),
        format="%(message)s",
        stream=sys.stderr,
        force=True,
    )
    # Tame chatty libs by default
    for noisy in ("urllib3", "github.Requester", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def bind(**ctx) -> None:
    """Bind key/value pairs to the current execution context.

    Example: ``bind(job_id=..., repo=...)`` — every subsequent log entry in the
    same coroutine/thread will include these fields.
    """
    structlog.contextvars.bind_contextvars(**ctx)


def unbind(*keys: str) -> None:
    structlog.contextvars.unbind_contextvars(*keys)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
