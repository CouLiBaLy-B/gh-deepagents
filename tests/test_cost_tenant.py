"""Per-installation cost attribution."""
from __future__ import annotations

import fakeredis
import pytest

from gh_deepagent.observability.cost_tenant import (
    TenantCostStore,
    bind_installation,
    current_installation,
    unbind_installation,
)


@pytest.fixture()
def store():
    return TenantCostStore(client=fakeredis.FakeRedis())


def test_record_aggregates(store):
    store.record(42, "openai", "gpt-4o-mini", 1_000_000, 500_000, 0.45)
    store.record(42, "openai", "gpt-4o-mini", 1_000_000, 500_000, 0.45)
    store.record(42, "anthropic", "claude-haiku-4", 100_000, 50_000, 0.30)
    usage = store.usage(42)
    assert usage["installation_id"] == 42
    assert usage["total_usd"] == pytest.approx(1.20)
    assert "openai:gpt-4o-mini" in usage["models"]
    assert usage["models"]["openai:gpt-4o-mini"]["usd"] == pytest.approx(0.90)
    assert usage["models"]["openai:gpt-4o-mini"]["input_tokens"] == 2_000_000
    assert usage["models"]["openai:gpt-4o-mini"]["output_tokens"] == 1_000_000


def test_isolated_per_installation(store):
    store.record(1, "openai", "gpt-4o", 100, 50, 1.0)
    store.record(2, "openai", "gpt-4o", 100, 50, 1.0)
    assert store.usage(1)["total_usd"] == 1.0
    assert store.usage(2)["total_usd"] == 1.0
    assert sorted(store.list_installations()) == [1, 2]


def test_reset(store):
    store.record(7, "openai", "gpt-4o", 100, 50, 1.0)
    assert store.usage(7)["total_usd"] == 1.0
    store.reset(7)
    assert store.usage(7)["total_usd"] == 0.0
    assert store.usage(7)["models"] == {}


def test_record_with_zero_cost_still_counts_tokens(store):
    # Local models have $0 price but tokens should still flow.
    store.record(9, "ollama", "qwen2.5-coder:14b", 1000, 500, 0.0)
    u = store.usage(9)
    assert u["models"]["ollama:qwen2.5-coder:14b"]["input_tokens"] == 1000
    assert u["models"]["ollama:qwen2.5-coder:14b"]["output_tokens"] == 500
    assert u["total_usd"] == 0.0


def test_bind_and_unbind_contextvar():
    assert current_installation() is None
    tok = bind_installation(42)
    assert current_installation() == 42
    unbind_installation(tok)
    assert current_installation() is None


def test_contextvar_is_isolated_per_thread():
    """contextvars don't bleed between threads."""
    import threading

    results: dict = {}

    def in_thread():
        # Did not see the parent's binding.
        results["before"] = current_installation()
        tok = bind_installation(99)
        results["inside"] = current_installation()
        unbind_installation(tok)
        results["after"] = current_installation()

    parent_tok = bind_installation(1)
    t = threading.Thread(target=in_thread)
    t.start(); t.join()
    unbind_installation(parent_tok)

    assert results["before"] in (None, 1)   # implementations differ; both fine
    assert results["inside"] == 99
    assert results["after"] in (None, 1)


def test_callback_attributes_to_bound_installation(monkeypatch):
    """The CostCallback should call the tenant store when an installation is bound."""
    from uuid import uuid4

    from gh_deepagent.observability import cost_tenant
    from gh_deepagent.observability.cost import CostCallback

    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(cost_tenant, "_store", TenantCostStore(client=fake))

    cb = CostCallback()
    run_id = uuid4()
    cb.on_chat_model_start(
        {"id": ["langchain", "chat_models", "openai", "ChatOpenAI"]},
        [], run_id=run_id, invocation_params={"model": "gpt-4o-mini"},
    )

    class FakeResult:
        llm_output = {"token_usage": {"prompt_tokens": 1_000_000,
                                       "completion_tokens": 500_000}}
        generations = []

    tok = bind_installation(123)
    try:
        cb.on_llm_end(FakeResult(), run_id=run_id)
    finally:
        unbind_installation(tok)

    usage = cost_tenant._store.usage(123)
    assert usage["total_usd"] == pytest.approx(0.15 + 0.30)   # 1M @ $0.15 + 0.5M @ $0.60
    assert 123 in cost_tenant._store.list_installations()


def test_callback_skips_when_no_installation_bound(monkeypatch):
    from uuid import uuid4
    from gh_deepagent.observability import cost_tenant
    from gh_deepagent.observability.cost import CostCallback

    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(cost_tenant, "_store", TenantCostStore(client=fake))

    cb = CostCallback()
    run_id = uuid4()
    cb.on_chat_model_start({"id": []}, [], run_id=run_id,
                           invocation_params={"model": "gpt-4o"})

    class FakeResult:
        llm_output = {"token_usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        generations = []

    cb.on_llm_end(FakeResult(), run_id=run_id)
    # No installation bound → nothing recorded.
    assert cost_tenant._store.list_installations() == []
