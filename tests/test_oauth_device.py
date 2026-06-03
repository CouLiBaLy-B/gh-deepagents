"""Unit tests for the GitHub Device Flow client."""
from __future__ import annotations

import httpx
import pytest

from gh_deepagent.dashboard.oauth import (
    DeviceFlowError,
    GitHubDeviceFlow,
)


class _MockGitHub(httpx.BaseTransport):
    def __init__(self, code_resp=None, poll_responses=None):
        self.code_resp = code_resp
        self.poll_responses = list(poll_responses or [])
        self.poll_calls = 0

    def handle_request(self, request):
        path = request.url.path
        if path == "/login/device/code":
            r = self.code_resp or httpx.Response(200, json={
                "device_code": "DEV-1", "user_code": "ABCD-1234",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900, "interval": 5,
            })
            return r
        if path == "/login/oauth/access_token":
            if not self.poll_responses:
                return httpx.Response(200, json={"error": "authorization_pending"})
            self.poll_calls += 1
            return self.poll_responses.pop(0)
        return httpx.Response(404)


def _make_flow(transport):
    flow = GitHubDeviceFlow(client_id="cid-test")
    flow._client = httpx.Client(base_url="https://github.com", transport=transport)
    return flow


def test_request_code_returns_bundle():
    flow = _make_flow(_MockGitHub())
    b = flow.request_code()
    assert b.user_code == "ABCD-1234"
    assert b.device_code == "DEV-1"
    assert b.interval == 5
    assert b.expired() is False


def test_poll_pending_returns_none():
    flow = _make_flow(_MockGitHub())
    b = flow.request_code()
    assert flow.poll_once(b) is None


def test_poll_success_returns_token():
    transport = _MockGitHub(poll_responses=[
        httpx.Response(200, json={
            "access_token": "gh_pat_xxx", "token_type": "bearer", "scope": "repo",
        }),
    ])
    flow = _make_flow(transport)
    b = flow.request_code()
    tok = flow.poll_once(b)
    assert tok is not None
    assert tok.access_token == "gh_pat_xxx"
    assert tok.scope == "repo"


def test_poll_slow_down_bumps_interval():
    transport = _MockGitHub(poll_responses=[
        httpx.Response(200, json={"error": "slow_down"}),
    ])
    flow = _make_flow(transport)
    b = flow.request_code()
    before = b.interval
    assert flow.poll_once(b) is None
    assert b.interval > before


def test_poll_terminal_error_raises():
    for err in ("expired_token", "access_denied", "device_flow_disabled"):
        transport = _MockGitHub(poll_responses=[
            httpx.Response(200, json={"error": err}),
        ])
        flow = _make_flow(transport)
        b = flow.request_code()
        with pytest.raises(DeviceFlowError) as exc:
            flow.poll_once(b)
        assert err in str(exc.value)


def test_missing_client_id_raises(monkeypatch):
    monkeypatch.delenv("DEEPAGENT_OAUTH_CLIENT_ID", raising=False)
    with pytest.raises(DeviceFlowError):
        GitHubDeviceFlow()


def test_request_code_handles_http_error():
    transport = _MockGitHub(code_resp=httpx.Response(403, text="forbidden"))
    flow = _make_flow(transport)
    with pytest.raises(DeviceFlowError):
        flow.request_code()
