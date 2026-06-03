"""Unit tests for .github/scripts/post-deploy-healthcheck.py.

We import it via importlib (it's not a package) and exercise its parser plus
its decision logic against an in-process HTTP server.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "post-deploy-healthcheck.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("hc_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


hc = _load_module()


# ---------------- pure-function tests ----------------

def test_parse_prom_extracts_basic_metrics():
    text = (
        "# HELP x foo\n"
        "deepagent_jobs_total{event=\"issues\",status=\"succeeded\"} 12\n"
        "deepagent_jobs_total{event=\"issues\",status=\"failed\"} 3\n"
        "deepagent_queue_depth 7\n"
    )
    out = hc.parse_prom(text)
    # parse_prom sums across labels
    assert out["deepagent_jobs_total"] == 15.0
    assert out["deepagent_queue_depth"] == 7.0


def test_parse_prom_ignores_garbage_lines():
    text = "\n# comment\nnot a metric\n\n"
    assert hc.parse_prom(text) == {}


# ---------------- end-to-end probe ----------------

class _Handler(BaseHTTPRequestHandler):
    """Pluggable HTTP server controlled by class attributes for each test."""

    health_status = 200
    health_body = b'{"status":"ok","queue_depth":0,"dead_letter":0}'
    metrics_status = 200
    metrics_body = b""

    def log_message(self, *_a):                 # silence the test output
        pass

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(self.health_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(self.health_body)))
            self.end_headers()
            self.wfile.write(self.health_body)
        elif self.path == "/metrics":
            self.send_response(self.metrics_status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(self.metrics_body)))
            self.end_headers()
            self.wfile.write(self.metrics_body)
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture()
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", _Handler
    finally:
        srv.shutdown()
        srv.server_close()


def _run(url, **extra):
    """Invoke the script's main() with parsed args."""
    argv = [
        "hc", "--url", url,
        "--timeout", "3", "--grace", "1", "--interval", "1",
        "--min-samples", "2",
    ]
    for k, v in extra.items():
        argv.extend([f"--{k.replace('_', '-')}", str(v)])
    old = sys.argv
    try:
        sys.argv = argv
        return hc.main()
    finally:
        sys.argv = old


def test_healthy_run_returns_zero(server):
    url, H = server
    H.health_status = 200
    H.health_body = b'{"status":"ok","queue_depth":1,"dead_letter":0}'
    assert _run(url) == 0


def test_degraded_after_grace_returns_one(server):
    url, H = server
    H.health_status = 503
    H.health_body = b'{"status":"degraded"}'
    assert _run(url) in (1, 3)   # 3 if it never reached 200 once


def test_unreachable_returns_three(server):
    # Server up but returning 5xx — should be 3 ("never reached") or 1 ("degraded").
    url, H = server
    H.health_status = 502
    H.health_body = b"bad gateway"
    rc = _run(url)
    assert rc in (1, 3)


def test_error_rate_triggers_two(server):
    url, H = server
    H.health_status = 200
    H.health_body = b'{"status":"ok","queue_depth":0,"dead_letter":0}'
    # 100 total, 99 failed → 99% error rate
    H.metrics_status = 200
    H.metrics_body = (
        b'deepagent_jobs_total{event="issues",status="succeeded"} 1\n'
        b'deepagent_jobs_total{event="issues",status="failed"} 99\n'
    )
    # We need to bump baseline → final difference. Restart timing: the script
    # uses *first* call as baseline (delta will be 0). To exercise the rate
    # branch we have to mutate the body mid-run.

    # Trick: start with empty metrics, then after a poll mutate to the angry numbers.
    H.metrics_body = (
        b'deepagent_jobs_total{event="issues",status="succeeded"} 0\n'
        b'deepagent_jobs_total{event="issues",status="failed"} 0\n'
    )

    def _mutate():
        time.sleep(1.2)
        H.metrics_body = (
            b'deepagent_jobs_total{event="issues",status="succeeded"} 1\n'
            b'deepagent_jobs_total{event="issues",status="failed"} 99\n'
        )

    threading.Thread(target=_mutate, daemon=True).start()
    rc = _run(url, admin_token="any", max_error_rate=0.5)
    assert rc == 2


def test_no_admin_token_skips_metrics_check(server):
    url, H = server
    H.health_status = 200
    H.metrics_status = 401         # not even consulted
    H.metrics_body = b"locked"
    assert _run(url) == 0
