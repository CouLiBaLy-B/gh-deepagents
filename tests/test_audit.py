"""Audit log persistence."""
from __future__ import annotations

import fakeredis
import pytest

from gh_deepagent.observability.audit import AuditEvent, AuditStore, audit_log


@pytest.fixture()
def store(monkeypatch):
    s = AuditStore(client=fakeredis.FakeRedis())
    monkeypatch.setattr("gh_deepagent.observability.audit._store", s)
    return s


def test_append_then_tail(store):
    store.append(AuditEvent(actor="alice", via="github", action="role.grant",
                            target="bob", installation_id=10,
                            metadata={"role": "operator"}))
    tail = store.tail_global()
    assert len(tail) == 1
    assert tail[0]["actor"] == "alice"
    assert tail[0]["action"] == "role.grant"
    assert tail[0]["metadata"]["role"] == "operator"


def test_per_installation_index(store):
    store.append(AuditEvent(actor="a", via="github", action="x", installation_id=1))
    store.append(AuditEvent(actor="b", via="github", action="y", installation_id=2))
    store.append(AuditEvent(actor="c", via="github", action="z", installation_id=1))
    g = store.tail_global()
    one = store.tail_for_installation(1)
    two = store.tail_for_installation(2)
    assert len(g) == 3
    assert len(one) == 2
    assert len(two) == 1
    assert {e["actor"] for e in one} == {"a", "c"}


def test_audit_log_helper(store):
    audit_log(actor="root", via="admin_token", action="dlq.requeue",
              target="job-1", installation_id=42, reason="manual")
    tail = store.tail_global()
    assert tail[0]["target"] == "job-1"
    assert tail[0]["metadata"]["reason"] == "manual"


def test_metadata_truncated_for_huge_payloads(store):
    huge = "x" * 10_000
    store.append(AuditEvent(actor="a", via="github", action="x",
                            metadata={"big": huge}))
    tail = store.tail_global()
    s = str(tail[0]["metadata"])
    # Should not have the full 10k
    assert len(s) < 2000


def test_cap_enforced(store, monkeypatch):
    monkeypatch.setattr(AuditStore, "GLOBAL_CAP", 5)
    for i in range(10):
        store.append(AuditEvent(actor=f"u{i}", via="github", action="x"))
    g = store.tail_global(limit=100)
    assert len(g) == 5
    # Latest first
    assert g[0]["actor"] == "u9"
