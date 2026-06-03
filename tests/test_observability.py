"""Tests for the observability surface: structlog setup + Prometheus metrics."""
from __future__ import annotations

import json

from prometheus_client import generate_latest

from gh_deepagent.observability import (
    JOBS_TOTAL,
    TOOL_CALLS,
    TOOL_DURATION,
    setup_logging,
)
from gh_deepagent.observability.metrics import track_tool


def test_metrics_register_and_render():
    JOBS_TOTAL.labels("issues", "succeeded").inc()
    body = generate_latest().decode()
    assert "deepagent_jobs_total" in body
    assert 'event="issues"' in body
    assert 'status="succeeded"' in body


def test_track_tool_decorator_counts_ok():
    @track_tool("dummy")
    def ok():
        return 42

    assert ok() == 42
    body = generate_latest().decode()
    assert "deepagent_tool_calls_total" in body
    assert 'tool="dummy"' in body
    assert 'status="ok"' in body


def test_track_tool_decorator_counts_errors():
    @track_tool("boom")
    def explode():
        raise ValueError("x")

    import pytest as _pt
    with _pt.raises(ValueError):
        explode()
    body = generate_latest().decode()
    assert 'tool="boom"' in body
    assert 'status="error"' in body


def test_setup_logging_idempotent():
    # Should not raise when called twice.
    setup_logging()
    setup_logging()
