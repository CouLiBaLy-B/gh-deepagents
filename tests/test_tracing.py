"""OTel tracing: no-op when disabled, real spans when enabled (if SDK installed)."""
from __future__ import annotations

import pytest


def test_span_is_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("DEEPAGENT_OTEL_ENABLED", raising=False)
    from gh_deepagent.observability.tracing import span, setup_tracing, is_enabled
    setup_tracing()                # noop
    assert is_enabled() is False
    with span("test", foo="bar") as s:
        assert s is None


def test_current_traceparent_noop():
    from gh_deepagent.observability.tracing import current_traceparent
    # When tracing is disabled this just returns None.
    assert current_traceparent() in (None,)


def test_continue_from_works_without_traceparent():
    from gh_deepagent.observability.tracing import continue_from
    with continue_from(None, "x") as s:
        assert s is None


@pytest.mark.skipif(
    pytest.importorskip("opentelemetry", reason="OTel SDK not installed") is None,
    reason="OTel SDK not installed",
)
def test_setup_tracing_real_sdk(monkeypatch):
    """If the SDK is available, enabling should produce a real tracer."""
    pytest.importorskip("opentelemetry.sdk")
    pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc.trace_exporter")
    monkeypatch.setenv("DEEPAGENT_OTEL_ENABLED", "1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    # Reset module state.
    from gh_deepagent.observability import tracing as t
    t._ENABLED = False
    t._TRACER = None
    t.setup_tracing()
    assert t.is_enabled() is True
    with t.span("smoke") as s:
        assert s is not None
    # Reset to avoid leaking into other tests.
    t._ENABLED = False
    t._TRACER = None
