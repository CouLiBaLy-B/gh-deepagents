"""Custom AgentMiddleware that emits Prometheus metrics + OpenTelemetry spans
for every tool call and every model invocation.

This replaces the previous monkey-patching of each tool's ``.func`` (see the
old ``agent._instrument_tools``). Reasons to switch:

1. The middleware runs **once per request** at the same place the agent
   framework actually calls the model / tool, so the timing it records is the
   end-to-end wall time including LangGraph's framework overhead — not just
   the user function's body.
2. We get a unified interception point for **both** the sync and async paths.
3. Adding new middleware (rate-limiting, caching, redaction…) is now a
   one-line append in ``agent.py`` instead of an ever-growing decorator stack.
4. It is the supported extension point per the deepagents harness docs.
"""
from __future__ import annotations

import contextlib
import time
from typing import Any, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
)
from langchain.tools.tool_node import ToolCallRequest

from .metrics import LLM_CALLS, TOOL_CALLS, TOOL_DURATION
from .tracing import span


class MetricsMiddleware(AgentMiddleware):
    """Counts model + tool calls, observes their durations, traces them.

    Plays nice with the existing CostCallback on the model (which handles
    token+USD accounting): the middleware only counts calls, while the
    callback consumes the per-token usage payload.
    """

    # AgentMiddleware sets `name` from the class name when unset; this is
    # the canonical handle used by HarnessProfile.excluded_middleware.
    name = "MetricsMiddleware"

    # ---- tool interception ------------------------------------------------
    def wrap_tool_call(
        self,
        state: AgentState,
        request: ToolCallRequest,
        next_: Callable[[AgentState, ToolCallRequest], Any],
    ) -> Any:
        tool_name = getattr(request, "name", None) or getattr(
            getattr(request, "tool_call", None), "name", "unknown"
        )
        t0 = time.perf_counter()
        cm = span(f"tool.{tool_name}", **{"tool.name": tool_name})
        cm.__enter__()
        try:
            result = next_(state, request)
            TOOL_CALLS.labels(tool_name, "ok").inc()
            return result
        except Exception:
            TOOL_CALLS.labels(tool_name, "error").inc()
            raise
        finally:
            TOOL_DURATION.labels(tool_name).observe(time.perf_counter() - t0)
            with contextlib.suppress(Exception):
                cm.__exit__(None, None, None)

    async def awrap_tool_call(
        self,
        state: AgentState,
        request: ToolCallRequest,
        next_: Callable[[AgentState, ToolCallRequest], Any],
    ) -> Any:
        tool_name = getattr(request, "name", None) or getattr(
            getattr(request, "tool_call", None), "name", "unknown"
        )
        t0 = time.perf_counter()
        cm = span(f"tool.{tool_name}", **{"tool.name": tool_name})
        cm.__enter__()
        try:
            result = await next_(state, request)
            TOOL_CALLS.labels(tool_name, "ok").inc()
            return result
        except Exception:
            TOOL_CALLS.labels(tool_name, "error").inc()
            raise
        finally:
            TOOL_DURATION.labels(tool_name).observe(time.perf_counter() - t0)
            with contextlib.suppress(Exception):
                cm.__exit__(None, None, None)

    # ---- model interception -----------------------------------------------
    def wrap_model_call(
        self,
        state: AgentState,
        request: ModelRequest,
        next_: Callable[[AgentState, ModelRequest], ModelResponse],
    ) -> ModelResponse:
        # We don't double-count tokens here (CostCallback does that). We
        # only count model invocations + trace them, which the cost callback
        # is intentionally agnostic about.
        model_name = "unknown"
        try:
            model = getattr(request, "model", None)
            model_name = (
                getattr(model, "model_name", None)
                or getattr(model, "model", None)
                or "unknown"
            )
        except Exception:
            pass

        cm = span("model.call", **{"model": str(model_name)})
        cm.__enter__()
        try:
            response = next_(state, request)
            # Provider isn't always knowable here; record under "via_middleware"
            # so we can distinguish from the callback-based counter.
            LLM_CALLS.labels("via_middleware", str(model_name)).inc()
            return response
        finally:
            with contextlib.suppress(Exception):
                cm.__exit__(None, None, None)

    async def awrap_model_call(
        self,
        state: AgentState,
        request: ModelRequest,
        next_: Callable[[AgentState, ModelRequest], ModelResponse],
    ) -> ModelResponse:
        model_name = "unknown"
        try:
            model = getattr(request, "model", None)
            model_name = (
                getattr(model, "model_name", None)
                or getattr(model, "model", None)
                or "unknown"
            )
        except Exception:
            pass
        cm = span("model.call", **{"model": str(model_name)})
        cm.__enter__()
        try:
            response = await next_(state, request)
            LLM_CALLS.labels("via_middleware", str(model_name)).inc()
            return response
        finally:
            with contextlib.suppress(Exception):
                cm.__exit__(None, None, None)
