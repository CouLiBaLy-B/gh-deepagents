"""Token-based authentication & multi-tenant authorisation.

A request authenticates by presenting one of:

1. **GitHub user access token** (the one obtained via OAuth Device Flow). We
   validate it by calling ``GET /user`` and ``GET /user/installations``. The
   user is then authorised on every installation they have access to on GitHub.

2. **Admin API token** — a static value configured via ``DEEPAGENT_ADMIN_TOKEN``
   (comma-separated). Tokens here see everything (no scoping).

Validation results are cached for 5 min to avoid hammering the GitHub API. Token
strings themselves are never logged — only their SHA-256 prefix.

The webhook ``POST /webhook`` endpoint stays exempt: it is signed with HMAC by
GitHub and has no notion of "user".
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger(__name__)


# --------- env-driven config ---------

def _admin_tokens() -> set[str]:
    raw = os.getenv("DEEPAGENT_ADMIN_TOKEN", "") or os.getenv("DEEPAGENT_ADMIN_TOKENS", "")
    return {t.strip() for t in raw.split(",") if t.strip()}


def _admin_logins() -> set[str]:
    raw = os.getenv("DEEPAGENT_ADMIN_GITHUB_LOGINS", "")
    return {t.strip().lower() for t in raw.split(",") if t.strip()}


def _verify_disabled() -> bool:
    """If true, **every** request is accepted as anonymous admin. Dev only."""
    return os.getenv("DEEPAGENT_AUTH_DISABLED", "0").lower() in ("1", "true", "yes")


# --------- data ---------

@dataclass(frozen=True)
class UserContext:
    """The identity attached to a request after successful auth."""
    login: str                            # GitHub login or "admin:<prefix>"
    installation_ids: frozenset[int]      # GitHub App installs this user can access
    is_admin: bool = False                # True → bypass scoping
    via: str = "github"                   # "github" | "admin_token" | "anonymous"

    def can_see_installation(self, iid: Optional[int]) -> bool:
        if self.is_admin:
            return True
        if iid is None:
            # Job with no installation_id (PAT-mode legacy) — only admins see it.
            return False
        return iid in self.installation_ids


@dataclass
class _CacheEntry:
    ctx: UserContext
    expires_at: float


# --------- verifier ---------

class TokenVerifier:
    """Stateless wrapper around GitHub API calls + in-memory cache."""

    CACHE_TTL = 300  # seconds

    def __init__(self, http_client: Optional[httpx.Client] = None):
        self._http = http_client or httpx.Client(
            base_url="https://api.github.com", timeout=5.0,
            headers={"Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    # ---- public
    def verify(self, token: Optional[str]) -> Optional[UserContext]:
        """Return a :class:`UserContext` for the token, or ``None`` if invalid.

        Returns ``None`` instead of raising so callers (FastAPI dependencies)
        can map to a clean 401 response without leaking details.
        """
        if _verify_disabled():
            return UserContext(login="anonymous", installation_ids=frozenset(),
                               is_admin=True, via="anonymous")
        if not token:
            return None

        # Cache hit?
        cached = self._get_cached(token)
        if cached:
            return cached

        # Admin static token?
        if token in _admin_tokens():
            ctx = UserContext(
                login=f"admin:{_short(token)}", installation_ids=frozenset(),
                is_admin=True, via="admin_token",
            )
            self._cache_put(token, ctx)
            return ctx

        # GitHub user token.
        try:
            user = self._http.get("/user", headers={"Authorization": f"Bearer {token}"})
            if user.status_code != 200:
                return None
            login = (user.json().get("login") or "").lower()
            iids = self._fetch_installations(token)
            is_admin = login in _admin_logins()
            ctx = UserContext(
                login=login, installation_ids=frozenset(iids),
                is_admin=is_admin, via="github",
            )
            self._cache_put(token, ctx)
            return ctx
        except httpx.HTTPError as e:
            log.warning("github auth check failed: %s", e)
            return None

    def invalidate(self, token: str) -> None:
        with self._lock:
            self._cache.pop(_key(token), None)

    # ---- internal
    def _fetch_installations(self, token: str) -> list[int]:
        out: list[int] = []
        page = 1
        while True:
            r = self._http.get(
                "/user/installations",
                headers={"Authorization": f"Bearer {token}"},
                params={"per_page": 100, "page": page},
            )
            if r.status_code != 200:
                break
            data = r.json()
            for inst in data.get("installations", []):
                if inst.get("id") is not None:
                    out.append(int(inst["id"]))
            if len(data.get("installations", [])) < 100:
                break
            page += 1
            if page > 10:  # safety
                break
        return out

    def _get_cached(self, token: str) -> Optional[UserContext]:
        with self._lock:
            entry = self._cache.get(_key(token))
            if not entry:
                return None
            if entry.expires_at < time.time():
                self._cache.pop(_key(token), None)
                return None
            return entry.ctx

    def _cache_put(self, token: str, ctx: UserContext) -> None:
        with self._lock:
            self._cache[_key(token)] = _CacheEntry(ctx, time.time() + self.CACHE_TTL)
            if len(self._cache) > 1024:
                # Drop the oldest few entries.
                for k in list(self._cache.keys())[:512]:
                    self._cache.pop(k, None)


def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _short(token: str) -> str:
    return _key(token)[:8]


# ---- module-level singleton (created lazily) ----
_singleton: Optional[TokenVerifier] = None


def get_verifier() -> TokenVerifier:
    global _singleton
    if _singleton is None:
        _singleton = TokenVerifier()
    return _singleton


def reset_verifier() -> None:                       # test helper
    global _singleton
    _singleton = None
