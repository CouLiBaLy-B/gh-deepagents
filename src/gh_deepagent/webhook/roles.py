"""Per-installation role-based access control.

Three roles, with strict ordering ``viewer < operator < admin``:

- ``viewer``    — read jobs, logs, quotas of this installation
- ``operator``  — viewer + requeue failed jobs of this installation
- ``admin``     — operator + manage roles of this installation

A user's effective role on an installation is the **maximum** of:

1. Their explicit role from ``deepagent:role:<iid>`` (if set)
2. The implicit role granted by GitHub App access (``viewer``)
3. ``admin`` if the user is a global admin (``is_admin`` on UserContext)

Storage:
- ``deepagent:role:<installation_id>``    HASH  {login: role}
- ``deepagent:role_audit:<iid>``          LIST  (last 100 changes)

Global ``DEEPAGENT_ADMIN_TOKEN`` / ``DEEPAGENT_ADMIN_GITHUB_LOGINS`` still grant
unconditional access — useful for support / break-glass.
"""
from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import redis

log = logging.getLogger(__name__)


class Role(str, enum.Enum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"

    @classmethod
    def parse(cls, s: str | None) -> Optional["Role"]:
        if not s:
            return None
        s = str(s).lower().strip()
        for r in cls:
            if r.value == s:
                return r
        return None

    @property
    def rank(self) -> int:
        return {"viewer": 1, "operator": 2, "admin": 3}[self.value]

    def can(self, required: "Role") -> bool:
        return self.rank >= required.rank


@dataclass(frozen=True)
class RoleAssignment:
    login: str
    role: Role
    granted_by: str
    granted_at: float


class RoleStore:
    KEY_PREFIX = "deepagent:role"
    AUDIT_CAP = 100

    def __init__(self, client: Optional[redis.Redis] = None):
        self._r = client or redis.Redis.from_url(
            os.getenv("DEEPAGENT_REDIS_URL", "redis://localhost:6379/0")
        )

    def _key(self, iid: int | str) -> str:
        return f"{self.KEY_PREFIX}:{iid}"

    def _audit_key(self, iid: int | str) -> str:
        return f"{self.KEY_PREFIX}_audit:{iid}"

    def get(self, iid: int, login: str) -> Optional[Role]:
        try:
            v = self._r.hget(self._key(iid), login.lower())
            if v is None:
                return None
            return Role.parse(v.decode() if isinstance(v, bytes) else v)
        except redis.RedisError:
            return None

    def list(self, iid: int) -> dict[str, Role]:
        try:
            raw = self._r.hgetall(self._key(iid))
        except redis.RedisError:
            return {}
        out: dict[str, Role] = {}
        for k, v in raw.items():
            login = k.decode() if isinstance(k, bytes) else k
            r = Role.parse(v.decode() if isinstance(v, bytes) else v)
            if r:
                out[login] = r
        return out

    def set(self, iid: int, login: str, role: Role, granted_by: str) -> RoleAssignment:
        login = login.lower()
        pipe = self._r.pipeline()
        pipe.hset(self._key(iid), login, role.value)
        ts = time.time()
        pipe.lpush(self._audit_key(iid),
                   f"{ts}|set|{granted_by}|{login}|{role.value}")
        pipe.ltrim(self._audit_key(iid), 0, self.AUDIT_CAP - 1)
        pipe.execute()
        return RoleAssignment(login=login, role=role, granted_by=granted_by, granted_at=ts)

    def remove(self, iid: int, login: str, removed_by: str) -> bool:
        login = login.lower()
        pipe = self._r.pipeline()
        pipe.hdel(self._key(iid), login)
        pipe.lpush(self._audit_key(iid),
                   f"{time.time()}|remove|{removed_by}|{login}|")
        pipe.ltrim(self._audit_key(iid), 0, self.AUDIT_CAP - 1)
        deleted, _, _ = pipe.execute()
        return bool(deleted)

    def audit(self, iid: int, limit: int = 50) -> list[dict]:
        try:
            raw = self._r.lrange(self._audit_key(iid), 0, limit - 1)
        except redis.RedisError:
            return []
        out = []
        for b in raw:
            s = b.decode() if isinstance(b, bytes) else b
            parts = s.split("|", 4)
            if len(parts) != 5:
                continue
            ts, action, by, login, role = parts
            try:
                ts_f = float(ts)
            except ValueError:
                continue
            out.append({"timestamp": ts_f, "action": action, "by": by,
                        "login": login, "role": role or None})
        return out


# Module-level singleton
_store: Optional[RoleStore] = None


def get_role_store() -> RoleStore:
    global _store
    if _store is None:
        _store = RoleStore()
    return _store


def reset_role_store() -> None:  # test helper
    global _store
    _store = None


# ---------------------------------------------------------------- effective role

def effective_role(user, installation_id: int) -> Optional[Role]:
    """Resolve the user's effective role on an installation.

    Args:
        user: a :class:`UserContext` from ``auth_tokens``.
        installation_id: the installation in question.

    Returns:
        ``None`` if the user has no access at all.
    """
    # Global admins are admin everywhere.
    if user.is_admin:
        return Role.ADMIN
    # Must at least have GitHub App access.
    if installation_id not in user.installation_ids:
        return None
    explicit = get_role_store().get(installation_id, user.login)
    # Default to viewer if user has access but no explicit role.
    return explicit or Role.VIEWER
