"""Tests for build_agent wiring: skills, per-sub-agent cheap model, native
memory, and human-in-the-loop interrupts. We capture the kwargs handed to
`create_deep_agent` instead of compiling a real graph."""
from __future__ import annotations

from pathlib import Path

import pytest

import gh_deepagent.agent as agent_mod
from gh_deepagent.agent import CHEAP_ROLES, INTERRUPT_TOOLS, SKILLS_DIR, build_agent
from gh_deepagent.config import get_settings


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    return tmp_path


@pytest.fixture()
def capture(monkeypatch):
    """Patch create_deep_agent + build_model; return a dict that the call fills."""
    import inspect

    seen: dict = {}
    real_sig = inspect.signature(agent_mod.create_deep_agent)

    def fake_create_deep_agent(**kwargs):
        seen.update(kwargs)
        return object()

    # Preserve the real signature so agent._supports() still detects skills/
    # memory/interrupt_on support through the patched callable.
    fake_create_deep_agent.__signature__ = real_sig

    def fake_build_model(spec=None, **_):
        return f"MODEL[{spec or 'MAIN'}]"

    monkeypatch.setattr(agent_mod, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(agent_mod, "build_model", fake_build_model)
    return seen


def _fresh_settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


def test_skills_and_memory_wired_for_local(repo, capture, monkeypatch):
    _fresh_settings(monkeypatch)
    build_agent(repo_path=repo, repo_full_name="o/r")
    assert capture["skills"] == [str(SKILLS_DIR)]
    assert capture["memory"] == ["/AGENTS.md"]
    # every sub-agent gets the skills library (custom ones don't inherit it)
    for sa in capture["subagents"]:
        assert sa["skills"] == [str(SKILLS_DIR)]


def test_cheap_model_assigned_to_light_roles(repo, capture, monkeypatch):
    _fresh_settings(monkeypatch, DEEPAGENT_MODEL_CHEAP="openai:cheap")
    build_agent(repo_path=repo, repo_full_name="o/r")
    by_name = {sa["name"]: sa for sa in capture["subagents"]}
    for role in CHEAP_ROLES:
        assert by_name[role]["model"] == "MODEL[openai:cheap]", role
    # critical-path roles keep the (default) strong model → no override key
    assert "model" not in by_name["coder"]
    assert "model" not in by_name["debugger"]


def test_no_cheap_override_when_unset(repo, capture, monkeypatch):
    _fresh_settings(monkeypatch)  # DEEPAGENT_MODEL_CHEAP empty
    build_agent(repo_path=repo, repo_full_name="o/r")
    for sa in capture["subagents"]:
        assert "model" not in sa


def test_interactive_enables_interrupts(repo, capture, monkeypatch):
    _fresh_settings(monkeypatch)
    build_agent(repo_path=repo, repo_full_name="o/r", interactive=True)
    assert set(capture["interrupt_on"]) == set(INTERRUPT_TOOLS)
    assert "checkpointer" in capture


def test_non_interactive_has_no_interrupts(repo, capture, monkeypatch):
    _fresh_settings(monkeypatch)
    build_agent(repo_path=repo, repo_full_name="o/r", interactive=False)
    assert "interrupt_on" not in capture
