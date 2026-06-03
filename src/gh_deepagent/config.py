"""Centralised settings, loaded from env / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    # LLM
    model: str = field(default_factory=lambda: os.getenv("DEEPAGENT_MODEL", "ollama:qwen2.5-coder:14b"))
    ollama_base_url: str = field(default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))

    # GitHub
    github_token: str = field(default_factory=lambda: os.getenv("GITHUB_TOKEN", ""))
    default_repo: str = field(default_factory=lambda: os.getenv("DEEPAGENT_DEFAULT_REPO", ""))

    # Agent
    max_turns: int = field(default_factory=lambda: int(os.getenv("DEEPAGENT_MAX_TURNS", "40")))
    workdir: Path = field(default_factory=lambda: Path(os.getenv("DEEPAGENT_WORKDIR", "/tmp/gh-deepagent")))
    trigger_label: str = field(default_factory=lambda: os.getenv("DEEPAGENT_TRIGGER_LABEL", "deepagent"))
    review_label: str = field(default_factory=lambda: os.getenv("DEEPAGENT_REVIEW_LABEL", "deepagent-review"))
    command_prefix: str = field(default_factory=lambda: os.getenv("DEEPAGENT_COMMAND_PREFIX", "/deepagent"))

    def assert_ready(self) -> None:
        has_pat = bool(self.github_token)
        has_app = bool(
            os.getenv("DEEPAGENT_GITHUB_APP_ID")
            and (
                os.getenv("DEEPAGENT_GITHUB_APP_PRIVATE_KEY")
                or os.getenv("DEEPAGENT_GITHUB_APP_PRIVATE_KEY_PATH")
            )
        )
        if not (has_pat or has_app):
            raise RuntimeError(
                "No GitHub credentials. Set GITHUB_TOKEN (PAT) OR "
                "DEEPAGENT_GITHUB_APP_ID + DEEPAGENT_GITHUB_APP_PRIVATE_KEY[_PATH]."
            )
        self.workdir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
