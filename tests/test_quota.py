"""QuotaManager: sliding window + concurrent + overrides."""
from __future__ import annotations

import time

import fakeredis
import pytest

from gh_deepagent.queue.quota import QuotaDecision, QuotaManager


@pytest.fixture()
def fake():
    return fakeredis.FakeRedis()


def test_disabled_when_no_limits(fake, monkeypatch):
    for k in ("DEEPAGENT_QUOTA_HOUR", "DEEPAGENT_QUOTA_DAY", "DEEPAGENT_QUOTA_CONCURRENT"):
        monkeypatch.delenv(k, raising=False)
    qm = QuotaManager(client=fake)
    d = qm.check_and_consume(42)
    assert d.allowed is True


def test_hour_limit_enforced(fake, monkeypatch):
    monkeypatch.setenv("DEEPAGENT_QUOTA_HOUR", "3")
    qm = QuotaManager(client=fake)
    for _ in range(3):
        assert qm.check_and_consume(99).allowed is True
    d = qm.check_and_consume(99)
    assert d.allowed is False
    assert d.bucket == "hour"
    assert d.limit == 3
    assert d.current == 3
    assert d.retry_after_seconds > 0


def test_concurrent_limit_and_release(fake, monkeypatch):
    monkeypatch.setenv("DEEPAGENT_QUOTA_CONCURRENT", "2")
    qm = QuotaManager(client=fake)
    assert qm.check_and_consume(7).allowed
    assert qm.check_and_consume(7).allowed
    denied = qm.check_and_consume(7)
    assert denied.allowed is False
    assert denied.bucket == "concurrent"
    qm.release_concurrent(7)
    assert qm.check_and_consume(7).allowed


def test_release_does_not_go_negative(fake, monkeypatch):
    monkeypatch.setenv("DEEPAGENT_QUOTA_CONCURRENT", "1")
    qm = QuotaManager(client=fake)
    qm.release_concurrent(1)
    qm.release_concurrent(1)
    qm.release_concurrent(1)
    assert qm.check_and_consume(1).allowed


def test_per_installation_overrides(fake, monkeypatch):
    monkeypatch.setenv("DEEPAGENT_QUOTA_HOUR", "1")
    monkeypatch.setenv("DEEPAGENT_QUOTA_OVERRIDES", '{"vip":{"hour":10,"day":0,"concurrent":0}}')
    qm = QuotaManager(client=fake)
    # default user: limited to 1
    assert qm.check_and_consume("standard").allowed
    assert not qm.check_and_consume("standard").allowed
    # vip: 10
    for _ in range(10):
        assert qm.check_and_consume("vip").allowed
    assert not qm.check_and_consume("vip").allowed


def test_usage_reporting(fake, monkeypatch):
    monkeypatch.setenv("DEEPAGENT_QUOTA_HOUR", "5")
    monkeypatch.setenv("DEEPAGENT_QUOTA_CONCURRENT", "2")
    qm = QuotaManager(client=fake)
    qm.check_and_consume(11)
    qm.check_and_consume(11)
    u = qm.usage(11)
    assert u["hour"]["used"] == 2
    assert u["hour"]["limit"] == 5
    assert u["concurrent"]["used"] == 2
    assert u["concurrent"]["limit"] == 2


def test_missing_installation_id_allowed(fake, monkeypatch):
    monkeypatch.setenv("DEEPAGENT_QUOTA_HOUR", "1")
    qm = QuotaManager(client=fake)
    # No installation id (PAT mode) → never blocked.
    for _ in range(5):
        assert qm.check_and_consume(None).allowed
