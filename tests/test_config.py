import os

from gh_deepagent.config import Settings


def test_defaults(monkeypatch):
    for k in ("DEEPAGENT_MODEL", "GITHUB_TOKEN", "DEEPAGENT_MAX_TURNS"):
        monkeypatch.delenv(k, raising=False)
    s = Settings()
    assert s.model.startswith("ollama:")
    assert s.max_turns == 40


def test_env_override(monkeypatch):
    monkeypatch.setenv("DEEPAGENT_MODEL", "anthropic:claude-sonnet-4-5")
    monkeypatch.setenv("DEEPAGENT_MAX_TURNS", "10")
    s = Settings()
    assert s.model == "anthropic:claude-sonnet-4-5"
    assert s.max_turns == 10
