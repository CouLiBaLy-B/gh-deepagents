"""Integration tests for v0.6 endpoints: cost, roles, audit."""
from __future__ import annotations

import sys
import threading

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient


class _FakeGitHub(httpx.BaseTransport):
    def __init__(self, users):
        self.users = users
        self._lock = threading.Lock()

    def handle_request(self, request):
        with self._lock:
            auth = request.headers.get("Authorization", "")
            token = auth.replace("Bearer ", "", 1) if auth.startswith("Bearer ") else ""
            record = self.users.get(token)
            if record is None:
                return httpx.Response(401, text='{"message":"Bad credentials"}')
            login, iids = record
            if request.url.path == "/user":
                return httpx.Response(200, json={"login": login})
            if request.url.path == "/user/installations":
                return httpx.Response(200, json={
                    "installations": [{"id": i} for i in iids],
                })
            return httpx.Response(404)


@pytest.fixture()
def env(monkeypatch):
    fake_redis = fakeredis.FakeRedis()
    fake_gh = _FakeGitHub({
        "alice-tok": ("alice", [10]),
        "bob-tok":   ("bob",   [10]),
        "carol-tok": ("carol", [20]),
        "admin-tok": ("global-admin", []),
    })
    monkeypatch.setenv("DEEPAGENT_WEBHOOK_SECRET", "topsecret")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("DEEPAGENT_TRIGGER_LABEL", "deepagent")
    monkeypatch.setenv("DEEPAGENT_REVIEW_LABEL", "deepagent-review")
    monkeypatch.setenv("DEEPAGENT_COMMAND_PREFIX", "/deepagent")
    monkeypatch.setenv("DEEPAGENT_ADMIN_TOKEN", "admin-tok-static")
    monkeypatch.delenv("DEEPAGENT_AUTH_DISABLED", raising=False)
    monkeypatch.setattr("redis.Redis.from_url",
                        staticmethod(lambda *_a, **_kw: fake_redis))

    # Reset singletons.
    from gh_deepagent.webhook.auth_tokens import reset_verifier
    from gh_deepagent.webhook.roles import reset_role_store
    from gh_deepagent.observability.audit import reset_audit_store
    from gh_deepagent.observability.cost_tenant import reset_store
    reset_verifier(); reset_role_store(); reset_audit_store(); reset_store()

    # Patch the verifier's HTTP client to point at the fake GitHub.
    from gh_deepagent.webhook import auth_tokens as at
    monkeypatch.setattr(at.TokenVerifier, "__init__",
        lambda self, http_client=None:
            object.__setattr__(self, "_http", httpx.Client(
                base_url="https://api.github.com", transport=fake_gh)) or
            setattr(self, "_cache", {}) or
            setattr(self, "_lock", __import__("threading").Lock()))

    sys.modules.pop("gh_deepagent.webhook.server", None)
    from gh_deepagent.webhook.server import app
    return TestClient(app), fake_redis


# ===========================================================  ROLES

def test_set_role_requires_admin_on_install(env):
    client, _ = env
    # Bob is a viewer on install 10 (default) — cannot grant roles.
    r = client.put("/installations/10/roles/charlie",
                   params={"role": "operator"},
                   headers={"Authorization": "Bearer bob-tok"})
    assert r.status_code == 403


def test_global_admin_can_grant_roles(env):
    client, _ = env
    r = client.put("/installations/10/roles/bob",
                   params={"role": "operator"},
                   headers={"Authorization": "Bearer admin-tok-static"})
    assert r.status_code == 200
    body = r.json()
    assert body["login"] == "bob"
    assert body["role"] == "operator"


def test_role_promotes_user(env):
    client, _ = env
    # Promote Alice to admin on install 10.
    r = client.put("/installations/10/roles/alice",
                   params={"role": "admin"},
                   headers={"Authorization": "Bearer admin-tok-static"})
    assert r.status_code == 200
    # Now Alice can grant Bob operator.
    r = client.put("/installations/10/roles/bob",
                   params={"role": "operator"},
                   headers={"Authorization": "Bearer alice-tok"})
    assert r.status_code == 200


def test_list_roles_scoped(env):
    client, _ = env
    # Carol is on install 20, not 10.
    r = client.get("/installations/10/roles",
                   headers={"Authorization": "Bearer carol-tok"})
    assert r.status_code == 404
    r = client.get("/installations/10/roles",
                   headers={"Authorization": "Bearer alice-tok"})
    assert r.status_code == 200


def test_remove_role(env):
    client, _ = env
    client.put("/installations/10/roles/bob", params={"role": "operator"},
               headers={"Authorization": "Bearer admin-tok-static"})
    r = client.delete("/installations/10/roles/bob",
                      headers={"Authorization": "Bearer admin-tok-static"})
    assert r.status_code == 200
    # Idempotent
    r = client.delete("/installations/10/roles/bob",
                      headers={"Authorization": "Bearer admin-tok-static"})
    assert r.status_code == 404


def test_invalid_role_rejected(env):
    client, _ = env
    r = client.put("/installations/10/roles/bob", params={"role": "god"},
                   headers={"Authorization": "Bearer admin-tok-static"})
    assert r.status_code == 400


# ===========================================================  COST

def test_installation_cost_starts_empty(env):
    client, _ = env
    r = client.get("/installations/10/cost",
                   headers={"Authorization": "Bearer alice-tok"})
    assert r.status_code == 200
    assert r.json()["total_usd"] == 0.0
    assert r.json()["models"] == {}


def test_installation_cost_scoped(env):
    client, _ = env
    # Alice on 10 cannot read 20's cost.
    r = client.get("/installations/20/cost",
                   headers={"Authorization": "Bearer alice-tok"})
    assert r.status_code == 404


def test_reset_cost_requires_admin(env):
    client, fake_redis = env
    # Seed a cost entry.
    from gh_deepagent.observability.cost_tenant import TenantCostStore
    TenantCostStore(client=fake_redis).record(10, "openai", "gpt-4o", 100, 50, 0.5)

    # Viewer (default) cannot reset.
    r = client.post("/installations/10/cost/reset",
                    headers={"Authorization": "Bearer alice-tok"})
    assert r.status_code == 403

    # Global admin can.
    r = client.post("/installations/10/cost/reset",
                    headers={"Authorization": "Bearer admin-tok-static"})
    assert r.status_code == 200


# ===========================================================  AUDIT

def test_audit_global_admin_only(env):
    client, _ = env
    r = client.get("/audit", headers={"Authorization": "Bearer alice-tok"})
    assert r.status_code == 403
    r = client.get("/audit", headers={"Authorization": "Bearer admin-tok-static"})
    assert r.status_code == 200


def test_audit_per_installation_scoped(env):
    client, _ = env
    # Grant a role — this should produce an audit entry.
    client.put("/installations/10/roles/bob",
               params={"role": "operator"},
               headers={"Authorization": "Bearer admin-tok-static"})

    # Alice (viewer on 10) sees the audit for installation 10.
    r = client.get("/installations/10/audit",
                   headers={"Authorization": "Bearer alice-tok"})
    assert r.status_code == 200
    events = r.json()
    assert any(e["action"] == "role.grant" and e["target"] == "bob" for e in events)

    # Carol cannot see 10's audit.
    r = client.get("/installations/10/audit",
                   headers={"Authorization": "Bearer carol-tok"})
    assert r.status_code == 404


def test_dlq_requeue_now_allowed_for_operators(env):
    """Previously admin-only; operators on the right install can now requeue."""
    client, fake_redis = env

    # Seed a dead job on installation 10.
    from gh_deepagent.queue import Job, JobQueue, JobStatus
    q = JobQueue(); q._r = fake_redis
    job = Job.new(event="issues", repo_full_name="o/r",
                  payload={}, installation_id=10)
    q.enqueue(job)
    # Manually mark dead.
    q.claim("w", timeout=1)
    q.retry_or_dead(job, "boom")
    # Force a second failure so it lands in DLQ (max_attempts default is 3).
    for _ in range(5):
        q.retry_or_dead(job, "boom")

    # Promote Bob to operator on install 10.
    client.put("/installations/10/roles/bob", params={"role": "operator"},
               headers={"Authorization": "Bearer admin-tok-static"})

    # Bob can requeue.
    r = client.post(f"/dlq/{job.id}/requeue",
                    headers={"Authorization": "Bearer bob-tok"})
    assert r.status_code == 200


def test_installations_listing(env):
    client, _ = env
    r = client.get("/installations", headers={"Authorization": "Bearer alice-tok"})
    assert r.status_code == 200
    data = r.json()
    assert any(d["installation_id"] == 10 and d["role"] == "viewer" for d in data)
