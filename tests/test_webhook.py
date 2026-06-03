"""Smoke tests for the webhook server. Uses fakeredis (no real Redis)."""
from __future__ import annotations

import hashlib
import hmac
import json
import sys

import fakeredis
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("DEEPAGENT_WEBHOOK_SECRET", "topsecret")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("DEEPAGENT_TRIGGER_LABEL", "deepagent")
    monkeypatch.setenv("DEEPAGENT_REVIEW_LABEL", "deepagent-review")
    monkeypatch.setenv("DEEPAGENT_COMMAND_PREFIX", "/deepagent")
    # Disable auth on this fixture — webhook smoke tests don't care about it
    # (a dedicated suite, test_webhook_auth.py, covers tenant scoping).
    monkeypatch.setenv("DEEPAGENT_AUTH_DISABLED", "1")

    # Patch Redis BEFORE the server is imported.
    monkeypatch.setattr("redis.Redis.from_url", staticmethod(lambda *_a, **_kw: fakeredis.FakeRedis()))
    # Reset auth verifier singleton between tests.
    from gh_deepagent.webhook.auth_tokens import reset_verifier
    reset_verifier()
    sys.modules.pop("gh_deepagent.webhook.server", None)
    from gh_deepagent.webhook.server import app
    return TestClient(app)


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ----- liveness -----

def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["redis"] is True
    assert "queue_depth" in body


def test_metrics_endpoint(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "deepagent_queue_depth" in r.text


# ----- webhook signature & routing -----

def test_webhook_signature_rejected(client):
    body = json.dumps({"hello": "world"}).encode()
    r = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": "sha256=deadbeef"},
    )
    assert r.status_code == 401


def test_webhook_issue_labeled_enqueues(client):
    payload = {
        "action": "labeled",
        "repository": {"full_name": "octo/hello"},
        "label": {"name": "deepagent"},
        "issue": {
            "number": 1, "html_url": "https://github.com/octo/hello/issues/1",
            "labels": [{"name": "deepagent"}],
        },
        "installation": {"id": 4242},
    }
    body = json.dumps(payload).encode()
    r = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": _sign("topsecret", body),
            "X-GitHub-Delivery": "d-1",
        },
    )
    assert r.status_code == 200
    js = r.json()
    assert js["accepted"] is True
    assert "job_id" in js


def test_webhook_non_actionable_is_ignored(client):
    payload = {
        "action": "labeled",
        "repository": {"full_name": "octo/hello"},
        "label": {"name": "other-label"},
        "issue": {"number": 1, "labels": []},
    }
    body = json.dumps(payload).encode()
    r = client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": _sign("topsecret", body),
            "X-GitHub-Delivery": "d-noop",
        },
    )
    assert r.status_code == 200
    assert r.json().get("ignored") is True


def test_webhook_dedup(client):
    payload = {
        "action": "labeled",
        "repository": {"full_name": "octo/hello"},
        "label": {"name": "deepagent"},
        "issue": {"number": 1, "labels": [], "html_url": "https://github.com/octo/hello/issues/1"},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": "issues",
        "X-Hub-Signature-256": _sign("topsecret", body),
        "X-GitHub-Delivery": "d-dup",
    }
    r1 = client.post("/webhook", content=body, headers=headers)
    r2 = client.post("/webhook", content=body, headers=headers)
    assert r1.json()["accepted"] is True
    assert r2.json() == {"deduped": True}


def test_job_endpoint_404(client):
    r = client.get("/jobs/does-not-exist")
    assert r.status_code == 404


def test_quota_rejection_returns_429(monkeypatch):
    # Configure a strict quota then build a fresh client.
    monkeypatch.setenv("DEEPAGENT_WEBHOOK_SECRET", "topsecret")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("DEEPAGENT_TRIGGER_LABEL", "deepagent")
    monkeypatch.setenv("DEEPAGENT_REVIEW_LABEL", "deepagent-review")
    monkeypatch.setenv("DEEPAGENT_COMMAND_PREFIX", "/deepagent")
    monkeypatch.setenv("DEEPAGENT_QUOTA_HOUR", "1")
    monkeypatch.setenv("DEEPAGENT_AUTH_DISABLED", "1")
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr("redis.Redis.from_url", staticmethod(lambda *_a, **_kw: fake))
    from gh_deepagent.webhook.auth_tokens import reset_verifier
    reset_verifier()
    sys.modules.pop("gh_deepagent.webhook.server", None)
    from gh_deepagent.webhook.server import app
    c = TestClient(app)

    payload = {
        "action": "labeled",
        "repository": {"full_name": "octo/hello"},
        "label": {"name": "deepagent"},
        "issue": {"number": 1, "html_url": "https://github.com/octo/hello/issues/1", "labels": []},
        "installation": {"id": 999},
    }
    body = json.dumps(payload).encode()

    def post(delivery: str):
        return c.post("/webhook", content=body, headers={
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": _sign("topsecret", body),
            "X-GitHub-Delivery": delivery,
        })

    r1 = post("a")
    r2 = post("b")
    assert r1.status_code == 200
    assert r2.status_code == 429
    err = r2.json()["detail"]
    assert err["error"] == "quota_exceeded"
    assert err["bucket"] == "hour"
    assert r2.headers.get("retry-after") is not None


def test_quota_usage_endpoint(monkeypatch):
    monkeypatch.setenv("DEEPAGENT_WEBHOOK_SECRET", "topsecret")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("DEEPAGENT_QUOTA_HOUR", "5")
    monkeypatch.setenv("DEEPAGENT_AUTH_DISABLED", "1")
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr("redis.Redis.from_url", staticmethod(lambda *_a, **_kw: fake))
    from gh_deepagent.webhook.auth_tokens import reset_verifier
    reset_verifier()
    sys.modules.pop("gh_deepagent.webhook.server", None)
    from gh_deepagent.webhook.server import app
    c = TestClient(app)
    r = c.get("/installations/42/quota")
    assert r.status_code == 200
    js = r.json()
    assert js["installation_id"] == 42
    assert js["usage"]["hour"]["limit"] == 5
