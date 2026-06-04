"""LLM factory — resolves a DEEPAGENT_MODEL string into a LangChain BaseChatModel.

Every model returned is wired with the ``CostCallback`` so token/cost metrics
flow through the observability stack automatically.

Supported model specs:
    ollama:<model>              → ChatOllama, OLLAMA_BASE_URL
    openrouter:<model>          → ChatOpenAI with base_url=openrouter.ai
    anthropic:<model>           → langchain init_chat_model
    openai:<model>              → langchain init_chat_model
    google_genai:<model>        → langchain init_chat_model
    groq:<model>                → langchain init_chat_model
    <anything else with ":"> → forwarded to init_chat_model verbatim
"""
from __future__ import annotations

import os
from typing import Any

from langchain.chat_models import init_chat_model

from .config import get_settings


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


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


def _build_openrouter(model_name: str, **overrides: Any):
    """OpenRouter speaks the OpenAI HTTP API. Use ChatOpenAI with a custom base_url.

    Recommended HTTP headers (`HTTP-Referer` + `X-Title`) help OpenRouter rank
    your traffic; we set them from env vars when present.
    """
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as e:
        raise RuntimeError(
            "Provider 'openrouter' selected but the langchain-openai package "
            "isn't installed. Install with: `pip install langchain-openai`."
        ) from e

    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Configure it via the dashboard's "
            "⚙️ LLM Settings page or in the Space's Settings → Secrets."
        )
    extra_headers = {}
    if site := os.getenv("OPENROUTER_HTTP_REFERER"):
        extra_headers["HTTP-Referer"] = site
    if title := os.getenv("OPENROUTER_X_TITLE", "gh-deepagent"):
        extra_headers["X-Title"] = title

    params: dict[str, Any] = dict(
        model=model_name,
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
        temperature=0.0,
    )
    if extra_headers:
        params["default_headers"] = extra_headers
    params.update(overrides)
    return ChatOpenAI(**params)


def build_model(model_spec: str | None = None, **overrides: Any):
    """Return a LangChain chat model with observability callbacks attached."""
    settings = get_settings()
    spec = model_spec or settings.model

    if spec.startswith("ollama:"):
        try:
            from langchain_ollama import ChatOllama
        except ImportError as e:
            raise RuntimeError(
                "Provider 'ollama' selected (DEEPAGENT_MODEL=" + spec + ") but "
                "the langchain-ollama package isn't installed. Install with: "
                "`pip install langchain-ollama` (or `pip install -e .[ollama]` "
                "from the project root). On Hugging Face Spaces, redeploy after "
                "rebasing on the latest Dockerfile which includes all providers."
            ) from e
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

    if spec.startswith("openrouter:"):
        _, model_name = spec.split("openrouter:", 1)
        return _attach_callbacks(_build_openrouter(model_name, **overrides))

    # Everything else → LangChain's init_chat_model (handles anthropic, openai, …).
    try:
        return _attach_callbacks(init_chat_model(spec, temperature=0.0, **overrides))
    except ImportError as e:
        provider = spec.split(":", 1)[0] if ":" in spec else spec
        hint_pkg = {
            "anthropic":    "langchain-anthropic",
            "openai":       "langchain-openai",
            "google_genai": "langchain-google-genai",
            "groq":         "langchain-groq",
        }.get(provider, f"the package for provider '{provider}'")
        raise RuntimeError(
            f"Couldn't load provider '{provider}' (DEEPAGENT_MODEL={spec}). "
            f"Install with: `pip install {hint_pkg}`. On Hugging Face Spaces, "
            "redeploy after rebasing on the latest Dockerfile."
        ) from e
