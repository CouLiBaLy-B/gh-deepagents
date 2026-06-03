"""LLM token & cost accounting via a LangChain callback handler.

Hooks into every LLM call made by the agent / sub-agents and:

- counts input/output tokens → ``LLM_TOKENS`` Prometheus counter
- prices the call against a catalog → ``LLM_COST_USD`` counter
- logs a structured ``llm_call`` event with provider/model/tokens/usd

Prices are USD per **1 M tokens** and come from ``PRICE_CATALOG``. Override or
extend via the env var ``DEEPAGENT_PRICE_OVERRIDES`` (JSON map of
``"<provider>:<model>": {"input": <usd>, "output": <usd>}``).

Local models (Ollama, vLLM, llama.cpp) are priced at 0 by default — tokens are
still counted so you can graph throughput.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from .logging_setup import get_logger
from .metrics import LLM_COST_USD, LLM_CALLS, LLM_TOKENS

log = logging.getLogger(__name__)
_struct = get_logger("agent.llm")


# USD per 1M tokens. Source: provider pricing pages (update as needed).
PRICE_CATALOG: dict[str, dict[str, float]] = {
    # OpenAI
    "openai:gpt-4o":               {"input": 2.50,  "output": 10.00},
    "openai:gpt-4o-mini":          {"input": 0.15,  "output": 0.60},
    "openai:gpt-4.1":              {"input": 2.00,  "output": 8.00},
    "openai:gpt-4.1-mini":         {"input": 0.40,  "output": 1.60},
    "openai:gpt-5":                {"input": 5.00,  "output": 15.00},
    "openai:o4-mini":              {"input": 1.10,  "output": 4.40},
    # Anthropic
    "anthropic:claude-sonnet-4-5": {"input": 3.00,  "output": 15.00},
    "anthropic:claude-opus-4":     {"input": 15.00, "output": 75.00},
    "anthropic:claude-haiku-4":    {"input": 0.80,  "output": 4.00},
    # Google
    "google_genai:gemini-2.5-pro": {"input": 1.25,  "output": 10.00},
    "google_genai:gemini-2.5-flash": {"input": 0.075, "output": 0.30},
    # Local — counted, not billed
    "ollama:*":   {"input": 0.0, "output": 0.0},
    "vllm:*":     {"input": 0.0, "output": 0.0},
}


def _load_overrides() -> dict[str, dict[str, float]]:
    raw = os.getenv("DEEPAGENT_PRICE_OVERRIDES", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("DEEPAGENT_PRICE_OVERRIDES is not valid JSON; ignoring.")
        return {}


def price_for(provider: str, model: str) -> tuple[float, float]:
    """Return (input $/1M, output $/1M) for a given (provider, model).

    Resolution order: explicit override → exact match → wildcard `provider:*` → (0, 0).
    """
    overrides = _load_overrides()
    catalog = {**PRICE_CATALOG, **overrides}
    key = f"{provider}:{model}"
    if key in catalog:
        return catalog[key]["input"], catalog[key]["output"]
    wildcard = f"{provider}:*"
    if wildcard in catalog:
        return catalog[wildcard]["input"], catalog[wildcard]["output"]
    return 0.0, 0.0


def _provider_from_metadata(serialized: dict, kwargs: dict) -> tuple[str, str]:
    """Best-effort extraction of (provider, model) from LangChain callback args."""
    # 1. invocation_params (set by LangChain on most chat models)
    inv = kwargs.get("invocation_params") or {}
    model = inv.get("model") or inv.get("model_name") or ""
    # 2. serialized.id chain — e.g. ['langchain', 'chat_models', 'openai', 'ChatOpenAI']
    chain = serialized.get("id") or [] if serialized else []
    provider = ""
    for marker, name in (
        ("openai", "openai"),
        ("anthropic", "anthropic"),
        ("google", "google_genai"),
        ("ollama", "ollama"),
        ("vllm", "vllm"),
        ("groq", "groq"),
    ):
        if any(marker in (p or "").lower() for p in chain):
            provider = name
            break
    if not provider:
        # Fallback: scan kwargs
        for key in ("model_id", "deployment", "base_url"):
            v = (kwargs.get(key) or "")
            if "ollama" in str(v):
                provider = "ollama"; break
            if "openai" in str(v):
                provider = "openai"; break
    return provider or "unknown", model or "unknown"


class CostCallback(BaseCallbackHandler):
    """LangChain callback that pushes token + cost metrics for every LLM call."""

    # We don't need to block any other handler.
    run_inline = True

    def on_llm_start(
        self, serialized: dict, prompts: list[str], *, run_id: UUID, **kwargs: Any
    ) -> None:
        # Stash provider/model on the run so on_llm_end can read it back.
        provider, model = _provider_from_metadata(serialized, kwargs)
        self._stash(run_id, provider=provider, model=model)

    def on_chat_model_start(
        self, serialized: dict, messages: list, *, run_id: UUID, **kwargs: Any
    ) -> None:
        provider, model = _provider_from_metadata(serialized, kwargs)
        self._stash(run_id, provider=provider, model=model)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        usage = self._extract_usage(response)
        provider, model = self._pop(run_id)
        in_tok = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        if in_tok == 0 and out_tok == 0:
            return
        LLM_TOKENS.labels(provider, model, "input").inc(in_tok)
        LLM_TOKENS.labels(provider, model, "output").inc(out_tok)
        LLM_CALLS.labels(provider, model).inc()
        in_price, out_price = price_for(provider, model)
        cost = (in_tok / 1_000_000) * in_price + (out_tok / 1_000_000) * out_price
        if cost:
            LLM_COST_USD.labels(provider, model).inc(cost)
        # ---- per-tenant attribution (if an installation context is bound) ----
        try:
            from .cost_tenant import current_installation, get_store
            iid = current_installation()
            if iid is not None:
                get_store().record(iid, provider, model, in_tok, out_tok, cost)
        except Exception:
            log.exception("per-tenant cost record failed")
        _struct.info(
            "llm_call",
            provider=provider, model=model,
            input_tokens=in_tok, output_tokens=out_tok,
            usd=round(cost, 6),
        )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._pop(run_id)

    # ---- helpers
    _runs: dict[UUID, dict] = {}

    @classmethod
    def _stash(cls, run_id: UUID, **data) -> None:
        cls._runs[run_id] = data
        # Best-effort cap to avoid unbounded growth on long-lived processes.
        if len(cls._runs) > 1024:
            for k in list(cls._runs.keys())[:512]:
                cls._runs.pop(k, None)

    @classmethod
    def _pop(cls, run_id: UUID) -> tuple[str, str]:
        d = cls._runs.pop(run_id, {})
        return d.get("provider", "unknown"), d.get("model", "unknown")

    @staticmethod
    def _extract_usage(response: LLMResult) -> dict:
        """Pull token usage from llm_output (OpenAI/Anthropic) or per-generation
        ``usage_metadata`` (preferred LangChain ≥ 0.3 path)."""
        out = response.llm_output or {}
        usage = out.get("token_usage") or out.get("usage") or {}
        if usage:
            return usage
        for gens in (response.generations or []):
            for g in gens:
                msg = getattr(g, "message", None)
                meta = getattr(msg, "usage_metadata", None) if msg else None
                if meta:
                    return {
                        "input_tokens": meta.get("input_tokens", 0),
                        "output_tokens": meta.get("output_tokens", 0),
                    }
        return {}
