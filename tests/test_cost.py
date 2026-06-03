"""Cost tracking: price lookup + callback updates Prometheus counters."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from prometheus_client import generate_latest

from gh_deepagent.observability.cost import CostCallback, price_for


def test_price_exact_match():
    in_p, out_p = price_for("anthropic", "claude-sonnet-4-5")
    assert in_p == 3.0
    assert out_p == 15.0


def test_price_wildcard_ollama():
    assert price_for("ollama", "qwen2.5-coder:14b") == (0.0, 0.0)


def test_price_unknown_defaults_zero():
    assert price_for("nonexistent", "model") == (0.0, 0.0)


def test_price_overrides(monkeypatch):
    monkeypatch.setenv("DEEPAGENT_PRICE_OVERRIDES", '{"openai:my-ft":{"input":0.5,"output":2.0}}')
    in_p, out_p = price_for("openai", "my-ft")
    assert in_p == 0.5 and out_p == 2.0


def test_callback_counts_tokens_and_cost():
    cb = CostCallback()
    run_id = uuid4()
    # simulate start
    cb.on_chat_model_start(
        {"id": ["langchain", "chat_models", "openai", "ChatOpenAI"]},
        [], run_id=run_id, invocation_params={"model": "gpt-4o-mini"},
    )

    # Fake LLMResult with token usage in llm_output (OpenAI path).
    class FakeResult:
        llm_output = {"token_usage": {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}}
        generations = []

    cb.on_llm_end(FakeResult(), run_id=run_id)

    body = generate_latest().decode()
    assert "deepagent_llm_tokens_total" in body
    assert 'provider="openai"' in body
    assert 'model="gpt-4o-mini"' in body
    assert 'kind="input"' in body
    assert 'kind="output"' in body
    # Cost: 1M @ 0.15 + 0.5M @ 0.60 = 0.15 + 0.30 = 0.45
    assert "deepagent_llm_cost_usd_total" in body


def test_callback_handles_missing_usage():
    cb = CostCallback()
    run_id = uuid4()
    cb.on_chat_model_start({"id": []}, [], run_id=run_id, invocation_params={})

    class FakeResult:
        llm_output = {}
        generations = []

    # Must not raise even though we have no usage info.
    cb.on_llm_end(FakeResult(), run_id=run_id)


def test_callback_extracts_usage_metadata_path():
    """LangChain ≥ 0.3 path: usage on generation.message.usage_metadata."""
    cb = CostCallback()
    run_id = uuid4()
    cb.on_chat_model_start(
        {"id": ["langchain", "chat_models", "anthropic", "ChatAnthropic"]},
        [], run_id=run_id, invocation_params={"model": "claude-haiku-4"},
    )

    class _Msg:
        usage_metadata = {"input_tokens": 100, "output_tokens": 50}

    class _Gen:
        message = _Msg()

    class FakeResult:
        llm_output = None
        generations = [[_Gen()]]

    cb.on_llm_end(FakeResult(), run_id=run_id)
    body = generate_latest().decode()
    assert 'model="claude-haiku-4"' in body
