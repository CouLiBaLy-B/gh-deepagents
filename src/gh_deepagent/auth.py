"""GitHub authentication — supports PAT or GitHub App (multi-tenant).

Two flavors of credentials are recognised, in priority order:

1. **GitHub App**  — set `DEEPAGENT_GITHUB_APP_ID` + a private key (see below).
   Each repo is authenticated with a *fresh installation access token* that lasts
   ~1 h and is automatically refreshed. The webhook payload also includes the
   installation id, so we never need to walk installations at request time.

2. **Personal Access Token** — set `GITHUB_TOKEN`. Single-tenant, simpler for
   local CLI use.

The `GitHubCredentials` class is the single source of truth for *both* PyGithub
clients and HTTPS clone URLs.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from github import Auth, Github, GithubIntegration


# ---------- low-level helpers ----------

def _load_private_key() -> str | None:
    """Read the App private key from env (raw or file path)."""
    key = os.getenv("DEEPAGENT_GITHUB_APP_PRIVATE_KEY")
    if key:
        # support escaped newlines from .env files
        return key.replace("\\n", "\n")
    path = os.getenv("DEEPAGENT_GITHUB_APP_PRIVATE_KEY_PATH")
    if path and Path(path).is_file():
        return Path(path).read_text()
    return None


@dataclass
class _CachedToken:
    token: str
    expires_at: float       # epoch seconds
    installation_id: int


# ---------- public API ----------

class GitHubCredentials:
    """Single object the rest of the app uses for *any* GitHub auth need.

    - `client_for_repo(full_name)`        → authenticated PyGithub `Github` instance
    - `clone_token_for_repo(full_name)`   → string usable as `x-access-token:<TOKEN>@...`
    - `for_app_metadata()`                → app-level client (e.g. list installations)

    Tokens are cached and refreshed transparently 60 s before expiry.
    """

    SAFETY_MARGIN = 60  # refresh tokens that have <60s left

    def __init__(
        self,
        *,
        pat: Optional[str] = None,
        app_id: Optional[str] = None,
        private_key_pem: Optional[str] = None,
    ):
        self._pat = pat
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._lock = threading.Lock()
        self._installation_cache: dict[str, int] = {}     # repo -> installation_id
        self._token_cache: dict[int, _CachedToken] = {}   # installation_id -> token
        self._integration: GithubIntegration | None = None

        if not (self.is_app or self.is_pat):
            raise RuntimeError(
                "No GitHub credentials configured. Set either GITHUB_TOKEN "
                "or DEEPAGENT_GITHUB_APP_ID + DEEPAGENT_GITHUB_APP_PRIVATE_KEY[_PATH]."
            )

    # ---- introspection
    @property
    def is_app(self) -> bool:
        return bool(self._app_id and self._private_key_pem)

    @property
    def is_pat(self) -> bool:
        return bool(self._pat)

    @property
    def mode(self) -> str:
        return "app" if self.is_app else "pat"

    # ---- constructors
    @classmethod
    def from_env(cls) -> "GitHubCredentials":
        return cls(
            pat=os.getenv("GITHUB_TOKEN") or None,
            app_id=os.getenv("DEEPAGENT_GITHUB_APP_ID") or None,
            private_key_pem=_load_private_key(),
        )

    # ---- App-level
    def _get_integration(self) -> GithubIntegration:
        if not self.is_app:
            raise RuntimeError("Not configured as a GitHub App.")
        if self._integration is None:
            app_auth = Auth.AppAuth(int(self._app_id), self._private_key_pem)  # type: ignore[arg-type]
            self._integration = GithubIntegration(auth=app_auth)
        return self._integration

    def for_app_metadata(self) -> Github:
        """A Github client authenticated *as the App* (limited endpoints)."""
        if self.is_app:
            app_auth = Auth.AppAuth(int(self._app_id), self._private_key_pem)  # type: ignore[arg-type]
            return Github(auth=app_auth)
        return Github(auth=Auth.Token(self._pat))  # type: ignore[arg-type]

    # ---- Per-repo
    def installation_id_for_repo(self, full_name: str) -> int:
        """Resolve (and cache) the installation id covering `full_name`."""
        if full_name in self._installation_cache:
            return self._installation_cache[full_name]
        owner, repo = full_name.split("/", 1)
        integration = self._get_integration()
        try:
            install = integration.get_repo_installation(owner, repo)
        except Exception as e:  # pragma: no cover - depends on network
            raise RuntimeError(
                f"GitHub App is not installed on {full_name}. "
                f"Install it from your App's public page. ({e})"
            ) from e
        self._installation_cache[full_name] = install.id
        return install.id

    def remember_installation(self, full_name: str, installation_id: int) -> None:
        """Hint from a webhook payload — avoids one API roundtrip."""
        self._installation_cache[full_name] = installation_id

    def _installation_token(self, installation_id: int) -> str:
        """Return a fresh installation access token (cached, auto-refreshed)."""
        with self._lock:
            cached = self._token_cache.get(installation_id)
            if cached and cached.expires_at - time.time() > self.SAFETY_MARGIN:
                return cached.token
            integration = self._get_integration()
            access = integration.get_access_token(installation_id)
            # access.expires_at is a datetime (UTC); convert to epoch
            expires_at = access.expires_at.timestamp() if access.expires_at else time.time() + 3000
            self._token_cache[installation_id] = _CachedToken(
                token=access.token, expires_at=expires_at, installation_id=installation_id
            )
            return access.token

    # ---- High-level
    def client_for_repo(self, full_name: str) -> Github:
        """A Github client authenticated to operate on this specific repo."""
        if self.is_app:
            iid = self.installation_id_for_repo(full_name)
            return Github(auth=Auth.Token(self._installation_token(iid)))
        return Github(auth=Auth.Token(self._pat))  # type: ignore[arg-type]

    def clone_token_for_repo(self, full_name: str) -> str:
        """A raw token usable in an HTTPS clone URL (`x-access-token:<TOK>@github.com/...`)."""
        if self.is_app:
            iid = self.installation_id_for_repo(full_name)
            return self._installation_token(iid)
        return self._pat  # type: ignore[return-value]

    # ---- Process-wide singleton (used by the webhook to share install cache)
    _singleton: "GitHubCredentials | None" = None

    @classmethod
    def shared(cls) -> "GitHubCredentials":
        if cls._singleton is None:
            cls._singleton = cls.from_env()
        return cls._singleton

    @classmethod
    def reset_shared(cls) -> None:
        cls._singleton = None

    # ---- Lifecycle
    def invalidate(self, full_name: str | None = None) -> None:
        """Drop cached tokens (e.g. on 401). Call after a failed call."""
        with self._lock:
            if full_name and full_name in self._installation_cache:
                iid = self._installation_cache.pop(full_name)
                self._token_cache.pop(iid, None)
            elif full_name is None:
                self._installation_cache.clear()
                self._token_cache.clear()
