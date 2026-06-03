"""Per-installation LLM cost attribution.

The webhook's ``CostCallback`` already aggregates global token/USD counters.
This module adds a *per-installation* breakdown stored in Redis so we can
answer "how much did installation X spend this month?".

Storage (all keys prefixed ``deepagent:cost:``):

- ``deepagent:cost:by_install:<iid>``           HASH  {model: total_usd}
- ``deepagent:cost:by_install:<iid>:tokens``    HASH  {<model>:input / output: count}
- ``deepagent:cost:installs``                    SET   installation_ids known to us

A :class:`contextvars.ContextVar` binds the active ``installation_id`` for the
duration of a worker job. The :class:`CostCallback` reads it inside
``on_llm_end()``; if unset (e.g. CLI mode) we skip the per-tenant accounting.
"""
from __future__ import annotations

import contextvars
import logging
import os
from typing import Optional

import redis

log = logging.getLogger(__name__)

# ContextVar so concurrent jobs in the same process can't cross-contaminate.
_current_installation: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "deepagent_installation_id", default=None,
)


def bind_installation(installation_id: Optional[int]) -> contextvars.Token:
    """Bind the active installation id; return a token to pass to :func:`unbind_installation`."""
    return _current_installation.set(installation_id)


def unbind_installation(token: contextvars.Token) -> None:
    _current_installation.reset(token)


def current_installation() -> Optional[int]:
    return _current_installation.get()


# ---------------------------------------------------------------- store

class TenantCostStore:
    KEY_PREFIX = "deepagent:cost"

    def __init__(self, client: Optional[redis.Redis] = None):
        self._r = client or redis.Redis.from_url(
            os.getenv("DEEPAGENT_REDIS_URL", "redis://localhost:6379/0")
        )

    def _hash(self, iid: int | str) -> str:
        return f"{self.KEY_PREFIX}:by_install:{iid}"

    def _tokens_hash(self, iid: int | str) -> str:
        return f"{self.KEY_PREFIX}:by_install:{iid}:tokens"

    def _installs_set(self) -> str:
        return f"{self.KEY_PREFIX}:installs"

    def record(
        self,
        installation_id: int,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        usd: float,
    ) -> None:
        """Persist one LLM call's cost & tokens against an installation."""
        try:
            key = f"{provider}:{model}"
            pipe = self._r.pipeline()
            pipe.sadd(self._installs_set(), installation_id)
            if usd:
                pipe.hincrbyfloat(self._hash(installation_id), key, usd)
            pipe.hincrby(self._tokens_hash(installation_id), f"{key}:input", input_tokens)
            pipe.hincrby(self._tokens_hash(installation_id), f"{key}:output", output_tokens)
            pipe.execute()
        except redis.RedisError as e:  # pragma: no cover
            log.warning("tenant cost record failed: %s", e)

    def usage(self, installation_id: int | str) -> dict:
        """Return per-model breakdown for an installation."""
        try:
            usd_h = self._r.hgetall(self._hash(installation_id))
            tok_h = self._r.hgetall(self._tokens_hash(installation_id))
        except redis.RedisError as e:  # pragma: no cover
            return {"error": str(e), "total_usd": 0.0, "models": {}}
        models: dict[str, dict] = {}
        for k, v in usd_h.items():
            key = k.decode() if isinstance(k, bytes) else k
            try:
                models.setdefault(key, {})["usd"] = float(v)
            except ValueError:
                continue
        for k, v in tok_h.items():
            key = k.decode() if isinstance(k, bytes) else k
            try:
                base, kind = key.rsplit(":", 1)
            except ValueError:
                continue
            models.setdefault(base, {})[kind] = int(v)
        total_usd = sum(m.get("usd", 0.0) for m in models.values())
        return {
            "installation_id": int(installation_id),
            "total_usd": round(total_usd, 6),
            "models": {k: {
                "usd": round(v.get("usd", 0.0), 6),
                "input_tokens": int(v.get("input", 0)),
                "output_tokens": int(v.get("output", 0)),
            } for k, v in models.items()},
        }

    def list_installations(self) -> list[int]:
        try:
            raw = self._r.smembers(self._installs_set())
            out = []
            for b in raw:
                try:
                    out.append(int(b.decode() if isinstance(b, bytes) else b))
                except (ValueError, AttributeError):
                    continue
            return sorted(out)
        except redis.RedisError:
            return []

    def reset(self, installation_id: int) -> None:
        """Admin helper to zero out an installation's cost (start a new billing period)."""
        pipe = self._r.pipeline()
        pipe.delete(self._hash(installation_id))
        pipe.delete(self._tokens_hash(installation_id))
        pipe.execute()


# Module-level singleton — the CostCallback uses this.
_store: Optional[TenantCostStore] = None


def get_store() -> TenantCostStore:
    global _store
    if _store is None:
        _store = TenantCostStore()
    return _store


def reset_store() -> None:  # test helper
    global _store
    _store = None
