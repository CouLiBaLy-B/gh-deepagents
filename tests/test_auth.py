"""Unit tests for GitHubCredentials.

We don't hit the GitHub API. Network paths are stubbed and we only check the
selection logic + caching behaviour.
"""
from __future__ import annotations

import time

import pytest

from gh_deepagent.auth import GitHubCredentials


# --- helpers ---

class _StubAccess:
    def __init__(self, token: str, expires_in: int = 3600):
        self.token = token
        # PyGithub returns a datetime; we emulate via a small shim
        import datetime as _dt
        self.expires_at = _dt.datetime.fromtimestamp(time.time() + expires_in)


class _StubIntegration:
    def __init__(self):
        self.calls = 0
        self.last_repo: tuple[str, str] | None = None

    def get_repo_installation(self, owner, repo):
        self.last_repo = (owner, repo)
        class _Install:
            id = 9999
        return _Install()

    def get_access_token(self, installation_id):
        self.calls += 1
        return _StubAccess(f"tok-{self.calls}")


# --- tests ---

def test_requires_credentials(monkeypatch):
    for k in (
        "GITHUB_TOKEN",
        "DEEPAGENT_GITHUB_APP_ID",
        "DEEPAGENT_GITHUB_APP_PRIVATE_KEY",
        "DEEPAGENT_GITHUB_APP_PRIVATE_KEY_PATH",
    ):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError):
        GitHubCredentials.from_env()


def test_pat_mode(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_abc")
    monkeypatch.delenv("DEEPAGENT_GITHUB_APP_ID", raising=False)
    creds = GitHubCredentials.from_env()
    assert creds.mode == "pat"
    assert creds.clone_token_for_repo("a/b") == "ghp_abc"


def test_app_mode_uses_installation_token(monkeypatch):
    monkeypatch.setenv("DEEPAGENT_GITHUB_APP_ID", "111")
    monkeypatch.setenv("DEEPAGENT_GITHUB_APP_PRIVATE_KEY", "-----BEGIN-----\nfake\n-----END-----\n")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    creds = GitHubCredentials.from_env()
    assert creds.mode == "app"
    stub = _StubIntegration()
    monkeypatch.setattr(creds, "_get_integration", lambda: stub)

    tok1 = creds.clone_token_for_repo("octo/hello")
    tok2 = creds.clone_token_for_repo("octo/hello")  # cached
    assert tok1 == tok2 == "tok-1"
    assert stub.calls == 1                 # second call hits cache
    assert stub.last_repo == ("octo", "hello")


def test_app_mode_installation_hint_skips_api(monkeypatch):
    monkeypatch.setenv("DEEPAGENT_GITHUB_APP_ID", "111")
    monkeypatch.setenv("DEEPAGENT_GITHUB_APP_PRIVATE_KEY", "-----BEGIN-----\nfake\n-----END-----\n")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    creds = GitHubCredentials.from_env()
    stub = _StubIntegration()
    monkeypatch.setattr(creds, "_get_integration", lambda: stub)

    creds.remember_installation("octo/hello", 4242)
    creds.clone_token_for_repo("octo/hello")
    # get_repo_installation must NOT have been called because we already had the id
    assert stub.last_repo is None
    assert stub.calls == 1


def test_app_mode_invalidate(monkeypatch):
    monkeypatch.setenv("DEEPAGENT_GITHUB_APP_ID", "111")
    monkeypatch.setenv("DEEPAGENT_GITHUB_APP_PRIVATE_KEY", "fakekey")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    creds = GitHubCredentials.from_env()
    stub = _StubIntegration()
    monkeypatch.setattr(creds, "_get_integration", lambda: stub)
    creds.remember_installation("octo/hello", 4242)
    creds.clone_token_for_repo("octo/hello")
    creds.invalidate("octo/hello")
    creds.clone_token_for_repo("octo/hello")  # forced to re-resolve
    assert stub.last_repo == ("octo", "hello")
    assert stub.calls == 2


def test_shared_singleton(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_xyz")
    GitHubCredentials.reset_shared()
    a = GitHubCredentials.shared()
    b = GitHubCredentials.shared()
    assert a is b
    GitHubCredentials.reset_shared()
