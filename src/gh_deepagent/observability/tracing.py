"""OpenTelemetry tracing — distributed traces across webhook → queue → worker → agent → tools.

Activated only when ``DEEPAGENT_OTEL_ENABLED=1`` and an OTLP endpoint is
configured (``OTEL_EXPORTER_OTLP_ENDPOINT``, e.g. ``http://tempo:4317``).

Spans emitted:

- ``webhook.receive``    HTTP request lifecycle (FastAPI auto-instr)
- ``job.enqueue``        webhook persists a Job into Redis
- ``job.dequeue``        worker claims a Job
- ``job.process``        worker runs the agent (parent for everything below)
- ``agent.stream``       per agent.stream() invocation
- ``tool.<name>``        every tool call from the agent
- ``llm.<provider>``     every LLM call (via langchain-otel auto-instrumentation if installed)

A ``traceparent`` header is propagated through the job payload so the worker's
span chains under the webhook's request span.
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)

_ENABLED = False
_TRACER = None


def is_enabled() -> bool:
    return _ENABLED


def setup_tracing(service_name: str = "gh-deepagent") -> None:
    """Initialise the OTel tracer provider + OTLP exporter.

    No-op when ``DEEPAGENT_OTEL_ENABLED`` is falsy. Safe to call multiple times.
    """
    global _ENABLED, _TRACER
    if _ENABLED:
        return
    if os.getenv("DEEPAGENT_OTEL_ENABLED", "0").lower() not in ("1", "true", "yes"):
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("opentelemetry deps not installed; tracing disabled")
        return

    resource = Resource.create({
        SERVICE_NAME: service_name,
        SERVICE_VERSION: os.getenv("DEEPAGENT_VERSION", "0.3.0"),
    })
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter()  # picks OTEL_EXPORTER_OTLP_ENDPOINT from env
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # Auto-instrument FastAPI + Redis when their instrumentors are present.
    for mod_name, instr_path in (
        ("fastapi", "opentelemetry.instrumentation.fastapi"),
        ("redis", "opentelemetry.instrumentation.redis"),
        ("requests", "opentelemetry.instrumentation.requests"),
        ("httpx", "opentelemetry.instrumentation.httpx"),
    ):
        try:
            instr_mod = __import__(instr_path, fromlist=["*"])
            instr_cls_name = f"{mod_name.capitalize()}Instrumentor"
            getattr(instr_mod, instr_cls_name)().instrument()
        except Exception:
            pass

    _TRACER = trace.get_tracer(service_name)
    _ENABLED = True

    # Inject trace IDs into structlog so logs are correlated.
    try:
        import structlog
        from opentelemetry import trace as _trace

        def add_trace_context(_logger, _method, event_dict):
            ctx = _trace.get_current_span().get_span_context()
            if ctx and ctx.is_valid:
                event_dict["trace_id"] = format(ctx.trace_id, "032x")
                event_dict["span_id"] = format(ctx.span_id, "016x")
            return event_dict

        # Patch the structlog config to add our processor in front of the renderer.
        current = structlog.get_config()
        processors = list(current["processors"])
        processors.insert(-1, add_trace_context)
        structlog.configure(processors=processors)
    except Exception:
        pass

    log.info("OpenTelemetry tracing enabled (service=%s)", service_name)


@contextlib.contextmanager
def span(name: str, **attrs: Any) -> Iterator[Optional[object]]:
    """Context manager that creates an OTel span when tracing is on; no-op otherwise.

    Always yields a span object (or None) so calling code stays identical.
    """
    if not _ENABLED or _TRACER is None:
        yield None
        return
    with _TRACER.start_as_current_span(name) as s:
        for k, v in attrs.items():
            if v is not None:
                try:
                    s.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))
                except Exception:
                    pass
        yield s


def current_traceparent() -> Optional[str]:
    """Return the W3C ``traceparent`` for the active span (for cross-process propagation)."""
    if not _ENABLED:
        return None
    try:
        from opentelemetry.propagate import inject
        carrier: dict[str, str] = {}
        inject(carrier)
        return carrier.get("traceparent")
    except Exception:
        return None


@contextlib.contextmanager
def continue_from(traceparent: Optional[str], name: str, **attrs: Any) -> Iterator[Optional[object]]:
    """Continue a trace from a serialised ``traceparent`` (set by the webhook)."""
    if not _ENABLED or _TRACER is None or not traceparent:
        with span(name, **attrs) as s:
            yield s
        return
    try:
        from opentelemetry import context as otel_context
        from opentelemetry.propagate import extract
        ctx = extract({"traceparent": traceparent})
        token = otel_context.attach(ctx)
        try:
            with span(name, **attrs) as s:
                yield s
        finally:
            otel_context.detach(token)
    except Exception:
        with span(name, **attrs) as s:
            yield s
