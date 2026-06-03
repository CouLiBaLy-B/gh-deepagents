"""Thin HTTP client over the webhook server's admin endpoints.

The dashboard talks to the SAME endpoints any external tool would — no privileged
access, no shared in-process state. Configure with env var ``DEEPAGENT_API_URL``.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import httpx


@dataclass
class APIError(Exception):
    status: int
    body: str

    def __str__(self) -> str:
        return f"HTTP {self.status}: {self.body[:200]}"


class WebhookAPI:
    """Synchronous client used by the Streamlit pages.

    All methods raise :class:`APIError` on non-2xx responses so the UI layer
    can show consistent error banners.

    Pass a ``token`` to authenticate as a GitHub user (bearer token). The
    server scopes responses to the installations that user has access to.
    """

    def __init__(self, base_url: Optional[str] = None, timeout: float = 10.0,
                 token: Optional[str] = None):
        self.base_url = (base_url or os.getenv("DEEPAGENT_API_URL", "http://localhost:8080")).rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, headers=headers)
        self._token = token

    # ---- helpers
    def _get(self, path: str, **params: Any) -> Any:
        try:
            r = self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise APIError(0, str(e)) from e
        if r.status_code >= 400:
            raise APIError(r.status_code, r.text)
        return r.json()

    def _post(self, path: str, **params: Any) -> Any:
        try:
            r = self._client.post(path, params=params)
        except httpx.HTTPError as e:
            raise APIError(0, str(e)) from e
        if r.status_code >= 400:
            raise APIError(r.status_code, r.text)
        return r.json()

    # ---- endpoints
    def healthz(self) -> dict:
        return self._get("/healthz")

    def whoami(self) -> dict:
        return self._get("/me")

    def metrics_raw(self) -> str:
        r = self._client.get("/metrics")
        if r.status_code >= 400:
            raise APIError(r.status_code, r.text)
        return r.text

    def list_jobs(self, limit_per_install: int = 50) -> list[dict]:
        return self._get("/jobs", limit_per_install=limit_per_install)

    def installation_jobs(self, installation_id: int | str, limit: int = 100) -> list[dict]:
        return self._get(f"/installations/{installation_id}/jobs", limit=limit)

    def job(self, job_id: str) -> dict:
        return self._get(f"/jobs/{job_id}")

    def job_logs(self, job_id: str, tail: int = 200) -> list[str]:
        return self._get(f"/jobs/{job_id}/logs", tail=tail).get("lines", [])

    def dlq(self, limit: int = 50) -> list[dict]:
        return self._get("/dlq", limit=limit)

    def requeue(self, job_id: str) -> dict:
        return self._post(f"/dlq/{job_id}/requeue")

    def installation_quota(self, installation_id: int | str) -> dict:
        return self._get(f"/installations/{installation_id}/quota")

    # ---- v0.6: cost, roles, audit
    def installations(self) -> list[dict]:
        return self._get("/installations")

    def installation_cost(self, installation_id: int | str) -> dict:
        return self._get(f"/installations/{installation_id}/cost")

    def reset_installation_cost(self, installation_id: int | str) -> dict:
        return self._post(f"/installations/{installation_id}/cost/reset")

    def list_roles(self, installation_id: int | str) -> dict:
        return self._get(f"/installations/{installation_id}/roles")

    def set_role(self, installation_id: int | str, login: str, role: str) -> dict:
        try:
            r = self._client.put(
                f"/installations/{installation_id}/roles/{login}",
                params={"role": role},
            )
        except httpx.HTTPError as e:
            raise APIError(0, str(e)) from e
        if r.status_code >= 400:
            raise APIError(r.status_code, r.text)
        return r.json()

    def remove_role(self, installation_id: int | str, login: str) -> dict:
        try:
            r = self._client.delete(f"/installations/{installation_id}/roles/{login}")
        except httpx.HTTPError as e:
            raise APIError(0, str(e)) from e
        if r.status_code >= 400:
            raise APIError(r.status_code, r.text)
        return r.json()

    def audit_global(self, limit: int = 200) -> list[dict]:
        return self._get("/audit", limit=limit)

    def installation_audit(self, installation_id: int | str, limit: int = 200) -> list[dict]:
        return self._get(f"/installations/{installation_id}/audit", limit=limit)

    # ---- SSE
    def stream_job(self, job_id: str, replay: bool = False) -> Iterator[tuple[str, str]]:
        """Yield (event_name, data) for each SSE message."""
        url = f"{self.base_url}/jobs/{job_id}/stream"
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        with httpx.stream("GET", url, params={"replay": str(replay).lower()},
                          timeout=None, headers=headers) as r:
            if r.status_code >= 400:
                raise APIError(r.status_code, r.read().decode("utf-8", "replace"))
            event, data_lines = "message", []
            for line in r.iter_lines():
                if line == "":
                    if data_lines:
                        yield event, "\n".join(data_lines)
                        event, data_lines = "message", []
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event: "):
                    event = line[len("event: "):].strip()
                elif line.startswith("data: "):
                    data_lines.append(line[len("data: "):])


# ----------------- Prometheus parsing ------------------------------------------------

def parse_prometheus(text: str) -> dict[str, list[dict]]:
    """Minimal text-format parser. Returns ``{metric_name: [{labels, value}]}``.

    Avoids pulling in ``prometheus_client.parser`` (which is overkill here) and
    keeps the dashboard dependency-light.
    """
    out: dict[str, list[dict]] = {}
    for raw in text.splitlines():
        if not raw or raw.startswith("#"):
            continue
        # `name{labels} value [timestamp]`
        try:
            if "{" in raw:
                name, rest = raw.split("{", 1)
                label_str, val_str = rest.rsplit("}", 1)
                value_str = val_str.strip().split()[0]
                labels = _parse_labels(label_str)
            else:
                parts = raw.split()
                name, value_str = parts[0], parts[1]
                labels = {}
            out.setdefault(name, []).append({"labels": labels, "value": float(value_str)})
        except Exception:
            continue
    return out


def _parse_labels(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    # naive parser, good enough for prom client output
    i = 0
    n = len(s)
    while i < n:
        # key
        j = s.find("=", i)
        if j == -1:
            break
        key = s[i:j].strip()
        # value (always quoted)
        if j + 1 >= n or s[j + 1] != '"':
            break
        k = j + 2
        while k < n and not (s[k] == '"' and s[k - 1] != "\\"):
            k += 1
        out[key] = s[j + 2 : k].replace('\\"', '"')
        i = k + 1
        if i < n and s[i] == ",":
            i += 1
    return out


def sum_by(values: list[dict], label: str) -> dict[str, float]:
    """Sum metric samples by a label key. Missing key → ``__total__``."""
    agg: dict[str, float] = {}
    for v in values:
        k = v["labels"].get(label, "__total__")
        agg[k] = agg.get(k, 0.0) + v["value"]
    return agg


def total(values: list[dict]) -> float:
    return sum(v["value"] for v in values)
