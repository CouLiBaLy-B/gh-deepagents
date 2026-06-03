"""Worker loop that drains the Redis queue and runs the agent on each job."""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from typing import Any, Callable, Optional

from .client import Job, JobQueue, JobStatus

log = logging.getLogger(__name__)


class Worker:
    """One Worker = one concurrent in-flight job. Run N workers to scale out."""

    def __init__(
        self,
        queue: Optional[JobQueue] = None,
        *,
        worker_id: Optional[str] = None,
        dispatch: Optional[Callable[[Job], dict[str, Any]]] = None,
        poll_timeout: int = 5,
    ):
        self.q = queue or JobQueue()
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self._stop = threading.Event()
        self._poll_timeout = poll_timeout
        self._dispatch = dispatch or self._default_dispatch

    # ---- lifecycle
    def run(self) -> None:
        """Block forever (until SIGTERM/SIGINT) draining the queue."""
        log.info("worker %s starting", self.worker_id)
        try:
            signal.signal(signal.SIGINT, self._handle_signal)
            signal.signal(signal.SIGTERM, self._handle_signal)
        except ValueError:
            # Signals can only be installed from the main thread. In multi-worker
            # mode the parent installs handlers and sets self._stop on receipt.
            pass
        n = self.q.recover_orphans(self.worker_id)
        if n:
            log.warning("worker %s recovered %d orphans", self.worker_id, n)
        while not self._stop.is_set():
            try:
                job = self.q.claim(self.worker_id, timeout=self._poll_timeout)
            except Exception:
                log.exception("claim failed; sleeping")
                time.sleep(2)
                continue
            if job is None:
                continue
            self._process(job)
        log.info("worker %s stopping", self.worker_id)

    def _handle_signal(self, *_a) -> None:
        log.info("worker %s got signal; draining", self.worker_id)
        self._stop.set()

    # ---- per-job
    def _process(self, job: Job) -> None:
        from ..observability.metrics import (
            JOBS_TOTAL, JOB_DURATION, IN_PROGRESS, QUEUE_DEPTH, DLQ_SIZE,
        )
        from ..observability.logging_setup import bind, unbind
        from ..observability.tracing import continue_from
        from ..observability.cost_tenant import bind_installation, unbind_installation
        from .quota import QuotaManager

        bind(job_id=job.id, repo=job.repo_full_name, gh_event=job.event)
        IN_PROGRESS.labels(self.worker_id).inc()
        quotas = QuotaManager(client=self.q._r)

        # Bind per-tenant context so the LLM callback can attribute spend.
        cost_token = bind_installation(job.installation_id)

        # Propagate distributed trace from the webhook.
        traceparent = (job.payload.get("_deepagent") or {}).get("traceparent")

        with continue_from(traceparent, "job.process",
                           **{"job.id": job.id, "repo": job.repo_full_name,
                              "gh_event": job.event, "worker": self.worker_id}):
            if not self.q.acquire_repo_lock(job.repo_full_name, owner=job.id):
                log.info("repo %s busy; requeueing", job.repo_full_name)
                time.sleep(2)
                self.q._r.lpush(self.q.QUEUE_KEY, job.id)
                self.q.ack(self.worker_id, job)
                JOBS_TOTAL.labels(job.event, "skipped").inc()
                IN_PROGRESS.labels(self.worker_id).dec()
                unbind("job_id", "repo", "gh_event")
                return

            self.q.update(job, status=JobStatus.RUNNING,
                          started_at=time.time(), attempts=job.attempts + 1)
            log.info("job started")
            t0 = time.perf_counter()
            try:
                result = self._dispatch(job)
                duration = time.perf_counter() - t0
                self.q.update(job, status=JobStatus.SUCCEEDED, result=result,
                              finished_at=time.time(), error=None)
                JOBS_TOTAL.labels(job.event, "succeeded").inc()
                JOB_DURATION.labels(job.event, "succeeded").observe(duration)
                log.info("job succeeded (%.2fs)", duration)
            except Exception as e:
                duration = time.perf_counter() - t0
                JOB_DURATION.labels(job.event, "failed").observe(duration)
                log.exception("job failed")
                requeued = self.q.retry_or_dead(job, error=f"{type(e).__name__}: {e}")
                JOBS_TOTAL.labels(job.event, "retried" if requeued else "dead").inc()
                if not requeued:
                    DLQ_SIZE.set(self.q.stats()["dead_letter"])
            finally:
                self.q.release_repo_lock(job.repo_full_name, owner=job.id)
                quotas.release_concurrent(job.installation_id)
                self.q.ack(self.worker_id, job)
                IN_PROGRESS.labels(self.worker_id).dec()
                QUEUE_DEPTH.set(self.q.stats()["queue_depth"])
                unbind("job_id", "repo", "gh_event")
                unbind_installation(cost_token)

    # ---- default dispatch (calls into the runner)
    @staticmethod
    def _default_dispatch(job: Job) -> dict[str, Any]:
        """Translate a Job into one of the runner entrypoints."""
        from .. import runner as r
        from ..auth import GitHubCredentials

        # Seed the installation id into the shared credentials cache, so the
        # runner doesn't need to look it up via the GitHub API.
        if job.installation_id is not None:
            GitHubCredentials.shared().remember_installation(
                job.repo_full_name, job.installation_id
            )

        payload = job.payload
        event = job.event
        repo_full = job.repo_full_name

        if event == "issues":
            res = r.fix_issue(payload["issue"]["html_url"])
            return {"action": "fix_issue", "ok": res.ok, "pr_url": res.pr_url}

        if event == "issue_comment":
            body = (payload["comment"]["body"] or "").strip()
            from ..config import get_settings
            prefix = get_settings().command_prefix
            instruction = body[len(prefix):].strip()
            is_pr = "pull_request" in payload["issue"]
            if is_pr:
                pr_number = payload["issue"]["number"]
                if instruction.lower().startswith("review"):
                    res = r.review_pr(repo_full, pr_number)
                    return {"action": "review_pr", "ok": res.ok, "pr_url": res.pr_url}
                res = r.iterate_pr(repo_full, pr_number, instruction)
                return {"action": "iterate_pr", "ok": res.ok, "pr_url": res.pr_url}
            res = r.evolve_code(repo_full, instruction)
            return {"action": "evolve", "ok": res.ok, "pr_url": res.pr_url}

        if event == "pull_request":
            res = r.review_pr(repo_full, payload["pull_request"]["number"])
            return {"action": "review_pr", "ok": res.ok}

        raise RuntimeError(f"unsupported event {event}")
