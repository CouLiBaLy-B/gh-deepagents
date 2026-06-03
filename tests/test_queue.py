"""Tests for the Redis-backed JobQueue and Worker.

Uses fakeredis so no real Redis is required.
"""
from __future__ import annotations

import threading
import time

import fakeredis
import pytest

from gh_deepagent.queue import Job, JobQueue, JobStatus, Worker


@pytest.fixture()
def queue(monkeypatch):
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr("redis.Redis.from_url", staticmethod(lambda *_a, **_kw: fake))
    q = JobQueue(url="redis://ignored")
    # Ensure all queue instances share the same fake redis backend.
    q._r = fake
    return q


def _make_job(**kw):
    base = dict(event="issues", repo_full_name="o/r", payload={"hello": "world"})
    base.update(kw)
    return Job.new(**base)


def test_enqueue_and_claim_roundtrip(queue):
    job = _make_job()
    queue.enqueue(job)
    claimed = queue.claim("w1", timeout=1)
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.payload == {"hello": "world"}


def test_dedup(queue):
    assert queue.already_seen("delivery-1") is False
    assert queue.already_seen("delivery-1") is True


def test_dedup_skips_when_empty(queue):
    assert queue.already_seen("") is False


def test_repo_lock_is_exclusive(queue):
    assert queue.acquire_repo_lock("o/r", owner="j1") is True
    assert queue.acquire_repo_lock("o/r", owner="j2") is False
    queue.release_repo_lock("o/r", owner="j1")
    assert queue.acquire_repo_lock("o/r", owner="j3") is True


def test_release_lock_only_if_owner(queue):
    queue.acquire_repo_lock("o/r", owner="j1")
    queue.release_repo_lock("o/r", owner="not-owner")        # no-op
    assert queue.acquire_repo_lock("o/r", owner="j2") is False  # still locked


def test_retry_then_dead(queue, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)       # speed test up
    queue.max_attempts = 2
    job = _make_job()
    queue.enqueue(job)
    queue.claim("w1", timeout=1)
    # attempt 1 fails → requeued
    assert queue.retry_or_dead(job, "boom") is True
    assert queue.stats()["queue_depth"] == 1
    # attempt 2 fails → dead
    assert queue.retry_or_dead(job, "boom2") is False
    s = queue.stats()
    assert s["dead_letter"] == 1
    assert s["queue_depth"] == 1   # the previous requeue is still pending; doesn't matter


def test_get_logs_capped(queue):
    queue.append_log("j1", "line-1")
    queue.append_log("j1", "line-2")
    assert queue.get_logs("j1") == ["line-1", "line-2"]


def test_requeue_dead(queue, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    queue.max_attempts = 1
    job = _make_job()
    queue.enqueue(job)
    queue.claim("w1", timeout=1)
    queue.retry_or_dead(job, "kaboom")
    assert queue.stats()["dead_letter"] == 1
    assert queue.requeue_dead(job.id) is True
    assert queue.stats()["dead_letter"] == 0
    assert queue.stats()["queue_depth"] >= 1


def test_recover_orphans(queue):
    job = _make_job()
    queue.enqueue(job)
    queue.claim("w1", timeout=1)              # job is now in w1's processing list
    # Simulate worker crash → orphans should come back to the main queue.
    recovered = queue.recover_orphans("w1")
    assert recovered == 1
    assert queue.stats()["queue_depth"] == 1


def test_per_installation_index(queue):
    j1 = _make_job(installation_id=10)
    j2 = _make_job(installation_id=10)
    j3 = _make_job(installation_id=20)
    j4 = _make_job(installation_id=None)        # PAT-mode legacy: no index entry
    for j in (j1, j2, j3, j4):
        queue.enqueue(j)
    ten = queue.list_for_installation(10, limit=10)
    twenty = queue.list_for_installation(20, limit=10)
    none = queue.list_for_installation("none", limit=10)
    assert {j.id for j in ten} == {j1.id, j2.id}
    assert {j.id for j in twenty} == {j3.id}
    assert none == []


def test_job_from_redis_roundtrip(queue):
    job = _make_job(installation_id=42, delivery_id="d-1")
    queue.enqueue(job)
    back = queue.get(job.id)
    assert back is not None
    assert back.installation_id == 42
    assert back.delivery_id == "d-1"
    assert back.status == JobStatus.PENDING


# ---------- Worker integration ----------

def test_worker_processes_one_job(queue, monkeypatch):
    job = _make_job()
    queue.enqueue(job)

    done = threading.Event()
    calls = []

    def fake_dispatch(j):
        calls.append(j.id)
        done.set()
        return {"ok": True}

    w = Worker(queue=queue, worker_id="test", dispatch=fake_dispatch, poll_timeout=1)
    t = threading.Thread(target=w.run, daemon=True)
    t.start()
    assert done.wait(timeout=10), "worker did not process job in time"
    # Give the finally-block a moment to commit the SUCCEEDED status.
    deadline = time.time() + 5
    while time.time() < deadline:
        if queue.get(job.id).status == JobStatus.SUCCEEDED:
            break
        time.sleep(0.05)
    w._stop.set()
    t.join(timeout=5)
    assert calls == [job.id]
    final = queue.get(job.id)
    assert final.status == JobStatus.SUCCEEDED
    assert final.result == {"ok": True}


def test_worker_dlq_on_repeated_failure(queue, monkeypatch):
    # Patch sleep ONLY on the worker module + the queue's retry helper, so the
    # test's own polling loop keeps real timing.
    import gh_deepagent.queue.client as _qc
    import gh_deepagent.queue.worker as _qw
    monkeypatch.setattr(_qc.time, "sleep", lambda _s: None)
    monkeypatch.setattr(_qw.time, "sleep", lambda _s: None)
    queue.max_attempts = 2
    job = _make_job()
    queue.enqueue(job)

    def boom(_):
        raise RuntimeError("nope")

    w = Worker(queue=queue, worker_id="test", dispatch=boom, poll_timeout=1)
    t = threading.Thread(target=w.run, daemon=True)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        time.sleep(0.05)
        if queue.stats()["dead_letter"] >= 1:
            break
    w._stop.set()
    t.join(timeout=5)
    assert queue.stats()["dead_letter"] == 1
    dead = queue.list_dead()
    assert dead[0].error and "nope" in dead[0].error
    assert dead[0].status == JobStatus.DEAD
