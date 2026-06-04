"""MetricsMiddleware: counts tool calls + observes durations + emits spans."""
from __future__ import annotations

from types import SimpleNamespace

import pytest


def _has_middleware_types() -> bool:
    try:
        from langchain.agents.middleware.types import AgentMiddleware  # noqa
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _has_middleware_types(),
    reason="LangChain middleware/types not importable in this environment",
)


def _fake_req(tool_name: str):
    """A ToolCallRequest-shaped object — middleware reads `name` or
    `tool_call.name`, nothing else."""
    return SimpleNamespace(name=tool_name, tool_call=SimpleNamespace(name=tool_name))


def test_middleware_counts_ok_calls():
    from prometheus_client import generate_latest
    from gh_deepagent.observability.middleware import MetricsMiddleware

    mw = MetricsMiddleware()
    state = {"messages": []}
    out = mw.wrap_tool_call(state, _fake_req("fake_tool"), lambda _s, _r: "ok-result")
    assert out == "ok-result"

    body = generate_latest().decode()
    assert 'tool="fake_tool"' in body
    assert 'status="ok"' in body


def test_middleware_counts_errors():
    from prometheus_client import generate_latest
    from gh_deepagent.observability.middleware import MetricsMiddleware

    mw = MetricsMiddleware()
    state = {"messages": []}

    def _boom(_state, _req):
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError):
        mw.wrap_tool_call(state, _fake_req("bad_tool"), _boom)

    body = generate_latest().decode()
    assert 'tool="bad_tool"' in body
    assert 'status="error"' in body


def test_middleware_has_canonical_name():
    from gh_deepagent.observability.middleware import MetricsMiddleware
    assert MetricsMiddleware.name == "MetricsMiddleware"


def test_middleware_records_duration_histogram():
    from prometheus_client import generate_latest
    from gh_deepagent.observability.middleware import MetricsMiddleware
    import time

    mw = MetricsMiddleware()
    state = {"messages": []}
    def _slow(_s, _r):
        time.sleep(0.01)
        return "ok"
    mw.wrap_tool_call(state, _fake_req("slow_tool"), _slow)

    body = generate_latest().decode()
    assert 'deepagent_tool_duration_seconds' in body
    assert 'tool="slow_tool"' in body
