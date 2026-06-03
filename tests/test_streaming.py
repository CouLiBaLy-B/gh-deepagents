"""SSE log streaming + pub/sub on append_log."""
from __future__ import annotations

import threading
import time

import fakeredis
import pytest

from gh_deepagent.queue import Job, JobQueue, JobStatus


@pytest.fixture()
def queue(monkeypatch):
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr("redis.Redis.from_url", staticmethod(lambda *_a, **_kw: fake))
    q = JobQueue(url="redis://ignored")
    q._r = fake
    return q


def test_append_log_publishes(queue):
    received: list[str] = []
    sub = queue._r.pubsub(ignore_subscribe_messages=True)
    sub.subscribe(queue._stream_chan("j1"))
    # give pubsub a moment to subscribe
    sub.get_message(timeout=0.1)

    queue.append_log("j1", "hello")
    # Drain
    for _ in range(20):
        msg = sub.get_message(timeout=0.1)
        if msg and msg.get("type") == "message":
            data = msg["data"]
            received.append(data.decode() if isinstance(data, bytes) else data)
            break
    sub.unsubscribe(); sub.close()
    assert "hello" in received


def test_publish_status_on_update(queue):
    job = Job.new(event="issues", repo_full_name="o/r", payload={})
    queue.enqueue(job)
    sub = queue._r.pubsub(ignore_subscribe_messages=True)
    sub.subscribe(queue._stream_chan(job.id))
    sub.get_message(timeout=0.1)

    queue.update(job, status=JobStatus.RUNNING)
    found = None
    for _ in range(20):
        msg = sub.get_message(timeout=0.1)
        if msg and msg.get("type") == "message":
            d = msg["data"]
            found = d.decode() if isinstance(d, bytes) else d
            break
    sub.unsubscribe(); sub.close()
    assert found is not None
    assert "_status" in found
    assert "running" in found


def test_logs_persist_alongside_pubsub(queue):
    queue.append_log("j2", "line-1")
    queue.append_log("j2", "line-2")
    assert queue.get_logs("j2") == ["line-1", "line-2"]
