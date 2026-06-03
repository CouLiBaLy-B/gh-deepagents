"""Per-installation sliding-window rate limiting backed by Redis.

Three configurable buckets, all opt-in via env vars:

- ``DEEPAGENT_QUOTA_HOUR``    — max jobs accepted per installation per hour    (default 0 = off)
- ``DEEPAGENT_QUOTA_DAY``     — max jobs per installation per day              (default 0 = off)
- ``DEEPAGENT_QUOTA_CONCURRENT`` — max concurrent in-flight jobs per installation (default 0 = off)

Per-installation overrides via the env var ``DEEPAGENT_QUOTA_OVERRIDES`` (JSON
map of ``"<installation_id>": {"hour": N, "day": N, "concurrent": N}``).

The webhook server consults :class:`QuotaManager.check_and_consume` BEFORE
enqueueing a job. If denied, it returns HTTP 429 with the limiting bucket name
+ retry-after seconds. The worker is responsible for calling
:meth:`release_concurrent` when a job finishes (regardless of outcome).

Implementation: ZSET per (bucket, installation_id) with score=timestamp. Old
entries are trimmed with ZREMRANGEBYSCORE before counting. Concurrent counter
is a plain INCR/DECR.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import redis

log = logging.getLogger(__name__)


@dataclass
class QuotaDecision:
    allowed: bool
    bucket: Optional[str] = None        # "hour" | "day" | "concurrent" if denied
    retry_after_seconds: int = 0
    current: int = 0
    limit: int = 0


class QuotaManager:
    KEY_PREFIX = "deepagent:quota"
    BUCKETS = {"hour": 3600, "day": 86400}

    def __init__(self, client: Optional[redis.Redis] = None):
        self._r = client or redis.Redis.from_url(
            os.getenv("DEEPAGENT_REDIS_URL", "redis://localhost:6379/0")
        )
        self._defaults = {
            "hour":       int(os.getenv("DEEPAGENT_QUOTA_HOUR", "0")),
            "day":        int(os.getenv("DEEPAGENT_QUOTA_DAY", "0")),
            "concurrent": int(os.getenv("DEEPAGENT_QUOTA_CONCURRENT", "0")),
        }
        self._overrides = self._load_overrides()

    @staticmethod
    def _load_overrides() -> dict[str, dict[str, int]]:
        raw = os.getenv("DEEPAGENT_QUOTA_OVERRIDES", "")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.warning("DEEPAGENT_QUOTA_OVERRIDES not valid JSON; ignoring.")
            return {}

    # ---- limits resolution
    def limits_for(self, installation_id: str | int | None) -> dict[str, int]:
        if installation_id is None:
            return dict(self._defaults)
        ov = self._overrides.get(str(installation_id), {})
        return {k: int(ov.get(k, self._defaults[k])) for k in self._defaults}

    # ---- key helpers
    def _bucket_key(self, bucket: str, installation_id: str) -> str:
        return f"{self.KEY_PREFIX}:{bucket}:{installation_id}"

    def _conc_key(self, installation_id: str) -> str:
        return f"{self.KEY_PREFIX}:concurrent:{installation_id}"

    # ---- public API
    def check_and_consume(self, installation_id: str | int | None) -> QuotaDecision:
        """Atomically check every enabled bucket; consume on success."""
        if installation_id is None or installation_id == "":
            return QuotaDecision(allowed=True)
        iid = str(installation_id)
        limits = self.limits_for(iid)

        # Concurrent first — cheapest, no scan.
        conc_limit = limits["concurrent"]
        if conc_limit > 0:
            current = int(self._r.get(self._conc_key(iid)) or 0)
            if current >= conc_limit:
                return QuotaDecision(False, "concurrent", retry_after_seconds=10,
                                     current=current, limit=conc_limit)

        # Sliding windows.
        now = time.time()
        for bucket, window in self.BUCKETS.items():
            lim = limits[bucket]
            if lim <= 0:
                continue
            key = self._bucket_key(bucket, iid)
            cutoff = now - window
            pipe = self._r.pipeline()
            pipe.zremrangebyscore(key, 0, cutoff)
            pipe.zcard(key)
            _, count = pipe.execute()
            count = int(count)
            if count >= lim:
                # Compute when the oldest entry will fall out of the window.
                oldest = self._r.zrange(key, 0, 0, withscores=True)
                retry = window
                if oldest:
                    _, ts = oldest[0]
                    retry = max(1, int(window - (now - float(ts))))
                return QuotaDecision(False, bucket, retry_after_seconds=retry,
                                     current=count, limit=lim)

        # All checks passed → consume.
        pipe = self._r.pipeline()
        for bucket, window in self.BUCKETS.items():
            if limits[bucket] > 0:
                key = self._bucket_key(bucket, iid)
                pipe.zadd(key, {f"{now}:{installation_id}": now})
                pipe.expire(key, window + 60)
        if conc_limit > 0:
            pipe.incr(self._conc_key(iid))
            pipe.expire(self._conc_key(iid), 3600)  # safety auto-reset
        pipe.execute()
        return QuotaDecision(True)

    def release_concurrent(self, installation_id: str | int | None) -> None:
        """Decrement the concurrent counter. Called by the worker on job completion."""
        if installation_id is None or installation_id == "":
            return
        iid = str(installation_id)
        if self.limits_for(iid)["concurrent"] <= 0:
            return
        try:
            val = self._r.decr(self._conc_key(iid))
            if int(val) < 0:
                self._r.set(self._conc_key(iid), 0)
        except redis.RedisError as e:  # pragma: no cover
            log.warning("release_concurrent failed: %s", e)

    def usage(self, installation_id: str | int) -> dict[str, dict]:
        """Inspection helper: current usage vs. limits for an installation."""
        iid = str(installation_id)
        limits = self.limits_for(iid)
        out: dict[str, dict] = {}
        now = time.time()
        for bucket, window in self.BUCKETS.items():
            key = self._bucket_key(bucket, iid)
            self._r.zremrangebyscore(key, 0, now - window)
            out[bucket] = {"used": int(self._r.zcard(key)), "limit": limits[bucket]}
        out["concurrent"] = {
            "used": int(self._r.get(self._conc_key(iid)) or 0),
            "limit": limits["concurrent"],
        }
        return out
