"""Tests for the dashboard's HTTP client + Prometheus text parser.

We mock httpx so no real webhook is required.
"""
from __future__ import annotations

import json

import httpx
import pytest

from gh_deepagent.dashboard.api import (
    APIError,
    WebhookAPI,
    parse_prometheus,
    sum_by,
    total,
)


# ----------------- Prometheus parser ---------------------------------------------

PROM_SAMPLE = """\
# HELP deepagent_jobs_total Total jobs by event and final status.
# TYPE deepagent_jobs_total counter
deepagent_jobs_total{event="issues",status="succeeded"} 12.0
deepagent_jobs_total{event="issues",status="failed"} 2.0
deepagent_jobs_total{event="issue_comment",status="succeeded"} 5.0
# HELP deepagent_queue_depth Pending jobs.
# TYPE deepagent_queue_depth gauge
deepagent_queue_depth 3
# HELP deepagent_llm_cost_usd_total Spend (USD).
# TYPE deepagent_llm_cost_usd_total counter
deepagent_llm_cost_usd_total{provider="openai",model="gpt-4o-mini"} 0.0457
"""


def test_parse_extracts_metrics():
    out = parse_prometheus(PROM_SAMPLE)
    assert "deepagent_jobs_total" in out
    assert "deepagent_queue_depth" in out
    assert len(out["deepagent_jobs_total"]) == 3


def test_parse_handles_no_labels():
    out = parse_prometheus(PROM_SAMPLE)
    qd = out["deepagent_queue_depth"]
    assert len(qd) == 1
    assert qd[0]["value"] == 3.0
    assert qd[0]["labels"] == {}


def test_parse_labels_complex():
    text = 'metric{a="x",b="y, z",c="quoted \\"inner\\""} 1\n'
    out = parse_prometheus(text)
    labels = out["metric"][0]["labels"]
    assert labels["a"] == "x"
    assert labels["c"] == 'quoted "inner"'


def test_sum_by_label():
    out = parse_prometheus(PROM_SAMPLE)
    by_status = sum_by(out["deepagent_jobs_total"], "status")
    assert by_status == {"succeeded": 17.0, "failed": 2.0}


def test_sum_by_missing_label_uses_total():
    samples = [{"labels": {}, "value": 5}, {"labels": {"x": "a"}, "value": 1}]
    assert sum_by(samples, "x") == {"__total__": 5, "a": 1}


def test_total_helper():
    out = parse_prometheus(PROM_SAMPLE)
    assert total(out["deepagent_jobs_total"]) == 19.0


def test_parse_skips_comments_and_blanks():
    assert parse_prometheus("# nothing\n\n") == {}


# ----------------- WebhookAPI -------------------------------------------------

class _MockTransport(httpx.BaseTransport):
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[httpx.Request] = []

    def handle_request(self, request):
        self.calls.append(request)
        path = request.url.path
        handler = self.routes.get((request.method, path))
        if handler is None:
            return httpx.Response(404, text=f"no route for {request.method} {path}")
        return handler(request)


def _make_api(routes):
    transport = _MockTransport(routes)
    api = WebhookAPI(base_url="http://test")
    api._client = httpx.Client(base_url="http://test", transport=transport)
    return api, transport


def test_healthz_ok():
    api, _ = _make_api({
        ("GET", "/healthz"): lambda r: httpx.Response(200, json={"status": "ok", "queue_depth": 0}),
    })
    assert api.healthz()["status"] == "ok"


def test_apierror_on_500():
    api, _ = _make_api({
        ("GET", "/healthz"): lambda r: httpx.Response(503, text="redis down"),
    })
    with pytest.raises(APIError) as exc:
        api.healthz()
    assert exc.value.status == 503
    assert "redis down" in exc.value.body


def test_job_endpoint():
    api, _ = _make_api({
        ("GET", "/jobs/abc"): lambda r: httpx.Response(200, json={"id": "abc", "status": "running"}),
    })
    assert api.job("abc")["status"] == "running"


def test_dlq_endpoint():
    rows = [{"id": "1", "event": "issues", "repo": "o/r", "error": "boom", "attempts": 3}]
    api, _ = _make_api({
        ("GET", "/dlq"): lambda r: httpx.Response(200, json=rows),
    })
    assert api.dlq() == rows


def test_requeue_endpoint():
    api, _ = _make_api({
        ("POST", "/dlq/1/requeue"): lambda r: httpx.Response(200, json={"requeued": True}),
    })
    assert api.requeue("1") == {"requeued": True}


def test_installation_quota():
    api, _ = _make_api({
        ("GET", "/installations/42/quota"):
            lambda r: httpx.Response(200, json={"installation_id": 42, "usage": {"hour": {"used": 1, "limit": 5}}}),
    })
    js = api.installation_quota(42)
    assert js["installation_id"] == 42
    assert js["usage"]["hour"]["used"] == 1


def test_metrics_raw_returns_text():
    api, _ = _make_api({
        ("GET", "/metrics"): lambda r: httpx.Response(200, text=PROM_SAMPLE),
    })
    txt = api.metrics_raw()
    assert "deepagent_queue_depth" in txt


def test_job_logs_returns_lines():
    api, _ = _make_api({
        ("GET", "/jobs/x/logs"): lambda r: httpx.Response(200, json={"lines": ["a", "b"]}),
    })
    assert api.job_logs("x") == ["a", "b"]
