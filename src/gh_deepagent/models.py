"""LLM factory — resolves a DEEPAGENT_MODEL string into a LangChain BaseChatModel.

Every model returned is wired with the ``CostCallback`` so token/cost metrics
flow through the observability stack automatically.
"""
from __future__ import annotations

from typing import Any

from langchain.chat_models import init_chat_model

from .config import get_settings


def _attach_callbacks(model):
    """Attach the cost callback to a chat model. Idempotent."""
    try:
        from .observability.cost import CostCallback
    except Exception:  # pragma: no cover
        return model
    cb = CostCallback()
    existing = list(getattr(model, "callbacks", None) or [])
    if any(isinstance(c, CostCallback) for c in existing):
        return model
    existing.append(cb)
    try:
        model.callbacks = existing
        return model
    except Exception:
        return model.with_config({"callbacks": existing})


def build_model(model_spec: str | None = None, **overrides: Any):
    """Return a LangChain chat model with observability callbacks attached."""
    settings = get_settings()
    spec = model_spec or settings.model

    if spec.startswith("ollama:"):
        from langchain_ollama import ChatOllama

        _, model_name = spec.split("ollama:", 1)
        params = dict(
            model=model_name,
            base_url=settings.ollama_base_url,
            temperature=0.0,
            num_ctx=32768,
            num_predict=4096,
        )
        params.update(overrides)
        return _attach_callbacks(ChatOllama(**params))

    return _attach_callbacks(init_chat_model(spec, temperature=0.0, **overrides))
