"""Multi-tenant scoping on the webhook server's admin API."""
from __future__ import annotations

import json
import sys

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient

from gh_deepagent.webhook.auth_tokens import TokenVerifier, UserContext, reset_verifier


# Helper: build a fake httpx transport that mocks GitHub's REST API.
class _FakeGitHub(httpx.BaseTransport):
    def __init__(self, users: dict[str, tuple[str, list[int]]]):
        # token -> (login, installation_ids)
        self.users = users

    def handle_request(self, request):
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


def _build_app(monkeypatch, *, admin_token="adm-1", admin_logins="", fake_github=None):
    monkeypatch.setenv("DEEPAGENT_ADMIN_TOKEN", admin_token)
    monkeypatch.setenv("DEEPAGENT_ADMIN_GITHUB_LOGINS", admin_logins)
    monkeypatch.delenv("DEEPAGENT_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("DEEPAGENT_WEBHOOK_SECRET", "topsecret")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("DEEPAGENT_TRIGGER_LABEL", "deepagent")
    monkeypatch.setenv("DEEPAGENT_REVIEW_LABEL", "deepagent-review")
    monkeypatch.setenv("DEEPAGENT_COMMAND_PREFIX", "/deepagent")
    monkeypatch.setattr("redis.Redis.from_url",
                        staticmethod(lambda *_a, **_kw: fakeredis.FakeRedis()))

    # Plug a fake GitHub client into the verifier.
    reset_verifier()
    if fake_github:
        from gh_deepagent.webhook import auth_tokens as at
        original_init = at.TokenVerifier.__init__

        def _patched(self, http_client=None):
            original_init(self, http_client=httpx.Client(
                base_url="https://api.github.com", transport=fake_github,
            ))

        monkeypatch.setattr(at.TokenVerifier, "__init__", _patched)

    sys.modules.pop("gh_deepagent.webhook.server", None)
    from gh_deepagent.webhook.server import app
    return TestClient(app)


# ----------------- auth basics -----------------

def test_unauthenticated_request_rejected(monkeypatch):
    client = _build_app(monkeypatch)
    r = client.get("/me")
    assert r.status_code == 401


def test_admin_token_grants_access(monkeypatch):
    client = _build_app(monkeypatch, admin_token="adm-xyz")
    r = client.get("/me", headers={"Authorization": "Bearer adm-xyz"})
    assert r.status_code == 200
    me = r.json()
    assert me["is_admin"] is True
    assert me["via"] == "admin_token"


def test_github_user_token_grants_scoped_access(monkeypatch):
    fake = _FakeGitHub({"gh-alice": ("alice", [10, 20])})
    client = _build_app(monkeypatch, fake_github=fake)
    r = client.get("/me", headers={"Authorization": "Bearer gh-alice"})
    assert r.status_code == 200
    me = r.json()
    assert me["login"] == "alice"
    assert me["is_admin"] is False
    assert me["installation_ids"] == [10, 20]


def test_invalid_token_rejected(monkeypatch):
    fake = _FakeGitHub({})       # no valid users
    client = _build_app(monkeypatch, fake_github=fake)
    r = client.get("/me", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_github_login_promoted_to_admin(monkeypatch):
    fake = _FakeGitHub({"gh-boss": ("Boss", [1])})
    client = _build_app(monkeypatch,
                        admin_logins="boss", fake_github=fake)
    r = client.get("/me", headers={"Authorization": "Bearer gh-boss"})
    assert r.json()["is_admin"] is True


def test_auth_disabled_lets_everything_through(monkeypatch):
    monkeypatch.setenv("DEEPAGENT_AUTH_DISABLED", "1")
    monkeypatch.setenv("DEEPAGENT_WEBHOOK_SECRET", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr("redis.Redis.from_url",
                        staticmethod(lambda *_a, **_kw: fakeredis.FakeRedis()))
    reset_verifier()
    sys.modules.pop("gh_deepagent.webhook.server", None)
    from gh_deepagent.webhook.server import app
    c = TestClient(app)
    r = c.get("/me")            # no token at all
    assert r.status_code == 200
    assert r.json()["is_admin"] is True


# ----------------- scoping on job endpoints -----------------

def _enqueue(monkeypatch, fake_redis, installation_id):
    """Helper: post a job via the webhook so it lands in fake_redis."""
    monkeypatch.setattr("redis.Redis.from_url",
                        staticmethod(lambda *_a, **_kw: fake_redis))
    sys.modules.pop("gh_deepagent.webhook.server", None)
    from gh_deepagent.webhook.server import app
    return TestClient(app)


def test_user_cannot_see_other_installation_jobs(monkeypatch):
    fake_redis = fakeredis.FakeRedis()
    fake_gh = _FakeGitHub({
        "alice-tok": ("alice", [10]),
        "bob-tok": ("bob", [20]),
    })

    monkeypatch.setenv("DEEPAGENT_WEBHOOK_SECRET", "topsecret")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("DEEPAGENT_TRIGGER_LABEL", "deepagent")
    monkeypatch.setenv("DEEPAGENT_REVIEW_LABEL", "deepagent-review")
    monkeypatch.setenv("DEEPAGENT_COMMAND_PREFIX", "/deepagent")
    monkeypatch.delenv("DEEPAGENT_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("DEEPAGENT_ADMIN_TOKEN", "")

    from gh_deepagent.webhook import auth_tokens as at
    reset_verifier()
    monkeypatch.setattr(at.TokenVerifier, "__init__",
        lambda self, http_client=None: object.__setattr__(self, "_http", httpx.Client(
            base_url="https://api.github.com", transport=fake_gh,
        )) or setattr(self, "_cache", {}) or setattr(self, "_lock", __import__("threading").Lock()))

    client = _enqueue(monkeypatch, fake_redis, installation_id=10)

    # Push a job for installation 10 (alice's).
    import hmac, hashlib
    payload = {
        "action": "labeled",
        "repository": {"full_name": "alice/repo"},
        "label": {"name": "deepagent"},
        "issue": {"number": 1, "html_url": "https://github.com/alice/repo/issues/1",
                  "labels": []},
        "installation": {"id": 10},
    }
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    r = client.post("/webhook", content=body, headers={
        "X-GitHub-Event": "issues",
        "X-Hub-Signature-256": sig,
        "X-GitHub-Delivery": "d-alice",
    })
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    # Alice sees her job.
    r = client.get(f"/jobs/{job_id}", headers={"Authorization": "Bearer alice-tok"})
    assert r.status_code == 200

    # Bob does NOT — we return 404 (not 403) to avoid leaking existence.
    r = client.get(f"/jobs/{job_id}", headers={"Authorization": "Bearer bob-tok"})
    assert r.status_code == 404


def test_dlq_endpoint_admin_only(monkeypatch):
    fake = _FakeGitHub({"alice": ("alice", [10])})
    client = _build_app(monkeypatch, admin_token="adm", fake_github=fake)
    # Regular user: 403
    r = client.get("/dlq", headers={"Authorization": "Bearer alice"})
    assert r.status_code == 403
    # Admin: 200
    r = client.get("/dlq", headers={"Authorization": "Bearer adm"})
    assert r.status_code == 200


def test_installation_quota_scoped(monkeypatch):
    fake = _FakeGitHub({"alice": ("alice", [10])})
    client = _build_app(monkeypatch, fake_github=fake)
    r = client.get("/installations/10/quota",
                   headers={"Authorization": "Bearer alice"})
    assert r.status_code == 200
    # 99 is not Alice's
    r = client.get("/installations/99/quota",
                   headers={"Authorization": "Bearer alice"})
    assert r.status_code == 404


def test_metrics_endpoint_admin_only(monkeypatch):
    fake = _FakeGitHub({"alice": ("alice", [10])})
    client = _build_app(monkeypatch, admin_token="adm", fake_github=fake)
    r = client.get("/metrics", headers={"Authorization": "Bearer alice"})
    assert r.status_code == 403
    r = client.get("/metrics", headers={"Authorization": "Bearer adm"})
    assert r.status_code == 200


def test_healthz_is_public(monkeypatch):
    client = _build_app(monkeypatch)
    r = client.get("/healthz")
    assert r.status_code == 200


def test_webhook_remains_public(monkeypatch):
    """The webhook itself must not require a user token — GitHub calls it."""
    client = _build_app(monkeypatch)
    import hmac, hashlib
    payload = {
        "action": "labeled",
        "repository": {"full_name": "o/r"},
        "label": {"name": "deepagent"},
        "issue": {"number": 1, "html_url": "https://github.com/o/r/issues/1", "labels": []},
    }
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    r = client.post("/webhook", content=body, headers={
        "X-GitHub-Event": "issues",
        "X-Hub-Signature-256": sig,
        "X-GitHub-Delivery": "d-pub",
    })
    assert r.status_code == 200
