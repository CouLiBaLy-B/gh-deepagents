"""Redis client wrapper providing the queue's data model.

Data model (all keys prefixed with ``deepagent:``):

- ``queue:default``               LIST  pending job IDs (FIFO via LPUSH/BRPOPLPUSH)
- ``queue:processing:<worker>``   LIST  jobs claimed by a worker (crash recovery)
- ``queue:dead``                  LIST  poison messages (job IDs)
- ``job:<id>``                    HASH  job metadata
- ``job:<id>:logs``               LIST  log lines, capped at 500
- ``dedup:<delivery_id>``         STRING TTL 10 min — webhook idempotency
- ``repo_lock:<repo>``            STRING TTL 30 min — per-repo mutex
- ``retries:<id>``                STRING counter
"""
from __future__ import annotations

import enum
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import redis

log = logging.getLogger(__name__)


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"          # exceeded retries
    SKIPPED = "skipped"    # dedup hit / repo locked


@dataclass
class Job:
    """Serializable unit of work pushed onto the queue."""
    id: str
    event: str
    repo_full_name: str
    payload: dict
    installation_id: Optional[int] = None
    delivery_id: Optional[str] = None
    status: JobStatus = JobStatus.PENDING
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    result: Optional[dict] = None

    @classmethod
    def new(
        cls,
        *,
        event: str,
        repo_full_name: str,
        payload: dict,
        installation_id: Optional[int] = None,
        delivery_id: Optional[str] = None,
    ) -> "Job":
        return cls(
            id=str(uuid.uuid4()),
            event=event,
            repo_full_name=repo_full_name,
            payload=payload,
            installation_id=installation_id,
            delivery_id=delivery_id,
        )

    def to_redis(self) -> dict[str, str]:
        d = asdict(self)
        d["payload"] = json.dumps(self.payload)
        d["result"] = json.dumps(self.result) if self.result is not None else ""
        d["status"] = self.status.value
        # Redis HSET only accepts str/bytes/int/float; coerce None/bool to str.
        return {k: ("" if v is None else str(v) if not isinstance(v, str) else v) for k, v in d.items()}

    @classmethod
    def from_redis(cls, d: dict[bytes | str, bytes | str]) -> "Job":
        def _s(key: str, default: str = "") -> str:
            v = d.get(key) or d.get(key.encode())
            if v is None:
                return default
            return v.decode() if isinstance(v, bytes) else str(v)

        return cls(
            id=_s("id"),
            event=_s("event"),
            repo_full_name=_s("repo_full_name"),
            payload=json.loads(_s("payload") or "{}"),
            installation_id=int(_s("installation_id")) if _s("installation_id") else None,
            delivery_id=_s("delivery_id") or None,
            status=JobStatus(_s("status", "pending")),
            attempts=int(_s("attempts", "0")),
            created_at=float(_s("created_at", "0") or 0),
            started_at=float(_s("started_at")) if _s("started_at") else None,
            finished_at=float(_s("finished_at")) if _s("finished_at") else None,
            error=_s("error") or None,
            result=json.loads(_s("result")) if _s("result") else None,
        )


class JobQueue:
    """Thin Redis-backed FIFO with idempotency, per-repo locking, retries and DLQ."""

    KEY_PREFIX = "deepagent"
    QUEUE_KEY = f"{KEY_PREFIX}:queue:default"
    DLQ_KEY = f"{KEY_PREFIX}:queue:dead"
    DEDUP_TTL = 600          # 10 min
    REPO_LOCK_TTL = 1800     # 30 min
    JOB_TTL = 24 * 3600      # 1 day
    LOG_CAP = 500
    MAX_ATTEMPTS = 3

    def __init__(self, url: Optional[str] = None, *, max_attempts: Optional[int] = None):
        self._url = url or os.getenv("DEEPAGENT_REDIS_URL", "redis://localhost:6379/0")
        self._r = redis.Redis.from_url(self._url)
        self.max_attempts = max_attempts or int(os.getenv("DEEPAGENT_MAX_ATTEMPTS", str(self.MAX_ATTEMPTS)))

    # ---- low-level keys
    def _job_key(self, jid: str) -> str: return f"{self.KEY_PREFIX}:job:{jid}"
    def _logs_key(self, jid: str) -> str: return f"{self.KEY_PREFIX}:job:{jid}:logs"
    def _dedup_key(self, did: str) -> str: return f"{self.KEY_PREFIX}:dedup:{did}"
    def _lock_key(self, repo: str) -> str: return f"{self.KEY_PREFIX}:repo_lock:{repo}"
    def _processing_key(self, worker: str) -> str: return f"{self.KEY_PREFIX}:queue:processing:{worker}"

    # ---- health
    def ping(self) -> bool:
        try:
            return bool(self._r.ping())
        except redis.RedisError:
            return False

    # ---- dedup
    def already_seen(self, delivery_id: str) -> bool:
        """Returns True iff this delivery was already processed (within TTL)."""
        if not delivery_id:
            return False
        # SET NX EX: atomic check-and-mark.
        return not bool(self._r.set(self._dedup_key(delivery_id), "1", nx=True, ex=self.DEDUP_TTL))

    # ---- enqueue
    def enqueue(self, job: Job) -> str:
        """Persist the job and push it on the queue. Returns the job id."""
        pipe = self._r.pipeline()
        pipe.hset(self._job_key(job.id), mapping=job.to_redis())
        pipe.expire(self._job_key(job.id), self.JOB_TTL)
        pipe.lpush(self.QUEUE_KEY, job.id)
        # Per-installation index (newest-first, capped) — used for tenant-scoped listings.
        if job.installation_id is not None:
            idx_key = self._install_index_key(job.installation_id)
            pipe.lpush(idx_key, job.id)
            pipe.ltrim(idx_key, 0, 999)
            pipe.expire(idx_key, self.JOB_TTL)
        pipe.execute()
        return job.id

    def _install_index_key(self, installation_id: int | str) -> str:
        return f"{self.KEY_PREFIX}:install_idx:{installation_id}"

    def list_for_installation(self, installation_id: int | str, limit: int = 100) -> list["Job"]:
        """Return the N most-recent jobs for an installation (newest first)."""
        raw = self._r.lrange(self._install_index_key(installation_id), 0, limit - 1)
        out: list[Job] = []
        for b in raw:
            jid = b.decode() if isinstance(b, bytes) else b
            j = self.get(jid)
            if j:
                out.append(j)
        return out

    # ---- dequeue (worker)
    def claim(self, worker_id: str, timeout: int = 5) -> Optional[Job]:
        """Atomically move a job from the pending queue to this worker's
        processing list (for crash recovery), then return it as a Job."""
        # BRPOPLPUSH atomically pops from QUEUE_KEY and pushes to processing list.
        raw = self._r.brpoplpush(self.QUEUE_KEY, self._processing_key(worker_id), timeout=timeout)
        if raw is None:
            return None
        jid = raw.decode() if isinstance(raw, bytes) else raw
        d = self._r.hgetall(self._job_key(jid))
        if not d:
            # Stale id in queue (TTL expired). Drop it.
            self._r.lrem(self._processing_key(worker_id), 1, jid)
            return None
        return Job.from_redis(d)

    def ack(self, worker_id: str, job: Job) -> None:
        """Remove from the processing list (job done, success or final failure)."""
        self._r.lrem(self._processing_key(worker_id), 1, job.id)

    def recover_orphans(self, worker_id: str) -> int:
        """Move any orphaned jobs back to the pending queue (crash recovery).

        Called on worker startup to recover from a previous crash of this worker.
        """
        n = 0
        while True:
            raw = self._r.rpoplpush(self._processing_key(worker_id), self.QUEUE_KEY)
            if raw is None:
                return n
            n += 1
            log.warning("recovered orphan job from %s", worker_id)

    # ---- per-repo locking
    def acquire_repo_lock(self, repo: str, owner: str) -> bool:
        """Try to grab an exclusive lock on this repo. TTL prevents deadlocks."""
        return bool(self._r.set(self._lock_key(repo), owner, nx=True, ex=self.REPO_LOCK_TTL))

    def release_repo_lock(self, repo: str, owner: str) -> None:
        """Release the lock iff we still own it (avoid releasing after TTL expiry).

        Uses Lua EVAL for atomicity on real Redis; falls back to a WATCH/MULTI/EXEC
        transaction when EVAL is unavailable (e.g. fakeredis in tests).
        """
        key = self._lock_key(repo)
        # Lua for compare-and-delete.
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end"
        )
        try:
            self._r.eval(script, 1, key, owner)
            return
        except redis.RedisError as e:  # pragma: no cover
            log.debug("release_repo_lock EVAL fallback: %s", e)
        # Fallback: WATCH-based transaction (slightly less atomic but acceptable).
        try:
            with self._r.pipeline() as pipe:
                while True:
                    try:
                        pipe.watch(key)
                        current = pipe.get(key)
                        if (current.decode() if isinstance(current, bytes) else current) != owner:
                            pipe.unwatch()
                            return
                        pipe.multi()
                        pipe.delete(key)
                        pipe.execute()
                        return
                    except redis.WatchError:
                        continue
        except Exception as e:  # pragma: no cover
            log.warning("release_repo_lock fallback failed: %s", e)

    # ---- status updates
    def update(self, job: Job, **fields: Any) -> None:
        for k, v in fields.items():
            setattr(job, k, v)
        self._r.hset(self._job_key(job.id), mapping=job.to_redis())
        self._r.expire(self._job_key(job.id), self.JOB_TTL)
        # Broadcast terminal/transition events to SSE subscribers.
        if "status" in fields:
            self.publish_status(job)

    def append_log(self, job_id: str, line: str) -> None:
        """Persist a log line and publish it on the per-job channel for SSE streaming."""
        truncated = line[:2000]
        key = self._logs_key(job_id)
        chan = self._stream_chan(job_id)
        pipe = self._r.pipeline()
        pipe.rpush(key, truncated)
        pipe.ltrim(key, -self.LOG_CAP, -1)
        pipe.expire(key, self.JOB_TTL)
        pipe.publish(chan, truncated)
        pipe.execute()

    def _stream_chan(self, job_id: str) -> str:
        return f"{self.KEY_PREFIX}:stream:{job_id}"

    def publish_status(self, job: "Job") -> None:
        """Broadcast a JSON status update to subscribers."""
        try:
            import json as _json
            self._r.publish(
                self._stream_chan(job.id),
                _json.dumps({"_status": job.status.value, "_error": job.error,
                             "_result": job.result, "_finished_at": job.finished_at}),
            )
        except Exception:  # pragma: no cover
            pass

    def subscribe_logs(self, job_id: str):
        """Yield log lines as they're published. Caller is responsible for breaking
        out of the loop once it sees a ``_status`` terminal message.
        """
        pubsub = self._r.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(self._stream_chan(job_id))
        try:
            for msg in pubsub.listen():
                data = msg.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                yield data
        finally:
            try:
                pubsub.unsubscribe()
                pubsub.close()
            except Exception:
                pass

    # ---- retry / DLQ
    def retry_or_dead(self, job: Job, error: str) -> bool:
        """Re-queue the job (with backoff) or move it to the DLQ.

        Returns True if re-queued, False if sent to DLQ.
        """
        job.attempts += 1
        if job.attempts >= self.max_attempts:
            self.update(job, status=JobStatus.DEAD, error=error, finished_at=time.time())
            self._r.lpush(self.DLQ_KEY, job.id)
            return False
        # exponential backoff: 2s, 8s, 32s ...
        delay = 2 ** (2 * job.attempts - 1)
        log.info("retrying job %s in %ds (attempt %d)", job.id, delay, job.attempts + 1)
        self.update(job, status=JobStatus.PENDING, error=error)
        # Cheap: rely on the worker pool sleep. Real backoff would use a delayed queue.
        time.sleep(min(delay, 10))   # cap to keep things responsive
        self._r.lpush(self.QUEUE_KEY, job.id)
        return True

    # ---- introspection
    def get(self, jid: str) -> Optional[Job]:
        d = self._r.hgetall(self._job_key(jid))
        return Job.from_redis(d) if d else None

    def get_logs(self, jid: str, tail: int = 200) -> list[str]:
        raw = self._r.lrange(self._logs_key(jid), -tail, -1)
        return [b.decode() if isinstance(b, bytes) else b for b in raw]

    def stats(self) -> dict[str, int]:
        return {
            "queue_depth": self._r.llen(self.QUEUE_KEY),
            "dead_letter": self._r.llen(self.DLQ_KEY),
        }

    def list_dead(self, limit: int = 50) -> list[Job]:
        raw = self._r.lrange(self.DLQ_KEY, 0, limit - 1)
        out = []
        for b in raw:
            jid = b.decode() if isinstance(b, bytes) else b
            j = self.get(jid)
            if j:
                out.append(j)
        return out

    def requeue_dead(self, jid: str) -> bool:
        """Move a DLQ job back to the pending queue and reset its attempts."""
        if self._r.lrem(self.DLQ_KEY, 1, jid) == 0:
            return False
        job = self.get(jid)
        if not job:
            return False
        self.update(job, status=JobStatus.PENDING, attempts=0, error=None)
        self._r.lpush(self.QUEUE_KEY, jid)
        return True
