"""Structured audit log persisted to Redis.

Every state-changing operation goes through :func:`audit_log`. We persist a
compact JSON record both in:

- A capped global list ``deepagent:audit`` (last 10 000 events)
- A per-installation index ``deepagent:audit:install:<iid>`` (last 1 000)

…and we ALSO emit a ``structlog`` event so the same data flows to Loki / your
log aggregator.

The :class:`AuditEvent` records:
- ``actor``: GitHub login or admin token prefix
- ``via``: ``github`` / ``admin_token`` / ``anonymous`` / ``system``
- ``action``: short verb, e.g. ``role.grant``, ``dlq.requeue``, ``job.create``
- ``target``: free-form descriptor (login affected, job id, repo)
- ``installation_id``: optional scope
- ``metadata``: free-form dict (truncated to 1 KB)

Querying for the dashboard's *Activity* page uses
:meth:`AuditStore.tail_global` / :meth:`AuditStore.tail_for_installation`.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

import redis

from .logging_setup import get_logger

log = logging.getLogger(__name__)
_struct = get_logger("audit")


@dataclass
class AuditEvent:
    actor: str
    via: str
    action: str
    target: str = ""
    installation_id: Optional[int] = None
    metadata: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        # Truncate metadata to 1 KB to keep records bounded.
        d = asdict(self)
        try:
            meta_s = json.dumps(d["metadata"], default=str)[:1024]
            d["metadata"] = json.loads(meta_s) if meta_s.startswith(("{", "[")) else meta_s
        except Exception:
            d["metadata"] = {"truncated": True}
        return json.dumps(d, default=str)


class AuditStore:
    GLOBAL_KEY = "deepagent:audit"
    PER_INSTALL_PREFIX = "deepagent:audit:install"
    GLOBAL_CAP = 10_000
    PER_INSTALL_CAP = 1_000

    def __init__(self, client: Optional[redis.Redis] = None):
        self._r = client or redis.Redis.from_url(
            os.getenv("DEEPAGENT_REDIS_URL", "redis://localhost:6379/0")
        )

    def _per_install_key(self, iid: int) -> str:
        return f"{self.PER_INSTALL_PREFIX}:{iid}"

    def append(self, event: AuditEvent) -> None:
        try:
            payload = event.to_json()
            pipe = self._r.pipeline()
            pipe.lpush(self.GLOBAL_KEY, payload)
            pipe.ltrim(self.GLOBAL_KEY, 0, self.GLOBAL_CAP - 1)
            if event.installation_id is not None:
                pipe.lpush(self._per_install_key(event.installation_id), payload)
                pipe.ltrim(self._per_install_key(event.installation_id), 0, self.PER_INSTALL_CAP - 1)
            pipe.execute()
        except redis.RedisError as e:  # pragma: no cover
            log.warning("audit persist failed: %s", e)
        # Always log too — single source of truth via structlog.
        _struct.info(
            "audit",
            actor=event.actor, via=event.via, action=event.action,
            target=event.target, installation_id=event.installation_id,
            **{f"meta_{k}": v for k, v in (event.metadata or {}).items()},
        )

    def tail_global(self, limit: int = 200) -> list[dict]:
        try:
            raw = self._r.lrange(self.GLOBAL_KEY, 0, limit - 1)
        except redis.RedisError:
            return []
        return self._decode(raw)

    def tail_for_installation(self, iid: int, limit: int = 200) -> list[dict]:
        try:
            raw = self._r.lrange(self._per_install_key(iid), 0, limit - 1)
        except redis.RedisError:
            return []
        return self._decode(raw)

    @staticmethod
    def _decode(raw: list) -> list[dict]:
        out = []
        for b in raw:
            s = b.decode() if isinstance(b, bytes) else b
            try:
                out.append(json.loads(s))
            except json.JSONDecodeError:
                continue
        return out


# Module-level singleton
_store: Optional[AuditStore] = None


def get_audit_store() -> AuditStore:
    global _store
    if _store is None:
        _store = AuditStore()
    return _store


def reset_audit_store() -> None:  # test helper
    global _store
    _store = None


# ---------------------------------------------------------------- helper

def audit_log(
    *,
    actor: str = "system",
    via: str = "system",
    action: str,
    target: str = "",
    installation_id: Optional[int] = None,
    **metadata,
) -> None:
    """Convenience wrapper used everywhere in the codebase."""
    get_audit_store().append(AuditEvent(
        actor=actor, via=via, action=action, target=target,
        installation_id=installation_id, metadata=metadata or {},
    ))
