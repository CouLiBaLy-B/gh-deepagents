"""OpenRouter provider wiring."""
from __future__ import annotations

import pytest

from gh_deepagent.observability.cost import price_for


def test_openrouter_known_model_priced():
    in_p, out_p = price_for("openrouter", "anthropic/claude-sonnet-4-5")
    assert in_p == 3.00 and out_p == 15.00


def test_openrouter_unknown_model_wildcard_zero():
    in_p, out_p = price_for("openrouter", "some/random-model")
    assert in_p == 0.0 and out_p == 0.0


def test_build_model_openrouter_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from gh_deepagent.models import build_model
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        build_model("openrouter:openai/gpt-4o-mini")


def test_build_model_openrouter_constructs_chatopenai(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_HTTP_REFERER", "https://example.com")
    from gh_deepagent.models import build_model, OPENROUTER_BASE_URL

    model = build_model("openrouter:anthropic/claude-haiku-4")
    # langchain-openai's ChatOpenAI is what we expect
    assert type(model).__name__ in {"ChatOpenAI", "ChatOpenAI"}
    # The model name should be the openrouter slug (vendor/model)
    assert getattr(model, "model_name", None) in {"anthropic/claude-haiku-4", None}
    # Either openai_api_base or base_url depending on SDK version
    base = (
        getattr(model, "openai_api_base", None)
        or str(getattr(model, "openai_base_url", None) or "")
        or str(getattr(getattr(model, "client", None), "base_url", "") or "")
    )
    assert OPENROUTER_BASE_URL in base


def test_missing_extra_produces_actionable_error(monkeypatch):
    """If langchain_ollama isn't installed, build_model should explain how to fix it."""
    monkeypatch.setenv("DEEPAGENT_MODEL", "ollama:qwen2.5-coder:14b")
    # Pretend the ollama package isn't installed by removing it from sys.modules
    # and inserting a fake that fails on import.
    import sys, importlib, builtins

    real_import = builtins.__import__
    def fake_import(name, *a, **kw):
        if name == "langchain_ollama":
            raise ImportError("no ollama for you")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    # Drop any cached settings so DEEPAGENT_MODEL is re-read.
    from gh_deepagent.config import get_settings
    get_settings.cache_clear()

    from gh_deepagent.models import build_model
    import pytest
    with pytest.raises(RuntimeError, match="langchain-ollama"):
        build_model()
