"""GitHub OAuth Device Flow — designed for the Streamlit dashboard.

Why Device Flow rather than the classic OAuth Authorization Code flow?

- Streamlit runs in any environment (local, server, behind reverse proxies)
  without exposing a redirect URI.
- Device Flow is the official GitHub-recommended path for CLIs and headless
  tools and works identically here.

How to enable in your GitHub OAuth App settings:
    Settings → Developer settings → OAuth Apps → New OAuth App
    Enable "Device Flow"
    Use the resulting Client ID in DEEPAGENT_OAUTH_CLIENT_ID.

This module is pure stdlib + httpx — no Streamlit imports. The UI piece lives
in ``dashboard/login.py``.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx

# Scopes requested. `repo` lets the bot read private repos the user has access
# to; `read:org` is needed to enumerate installations cleanly. Keep this list
# minimal — every extra scope is a privilege-escalation risk.
DEFAULT_SCOPES = "repo read:org read:user"


@dataclass
class DeviceCodeBundle:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int                         # seconds between polls
    obtained_at: float

    @property
    def expires_at(self) -> float:
        return self.obtained_at + self.expires_in

    def expired(self) -> bool:
        return time.time() > self.expires_at


@dataclass
class TokenBundle:
    access_token: str
    token_type: str
    scope: str
    obtained_at: float


class DeviceFlowError(Exception):
    pass


class GitHubDeviceFlow:
    """A 3-call dance: request code → user visits URL → poll for token."""

    def __init__(self, client_id: Optional[str] = None, scopes: str = DEFAULT_SCOPES):
        self.client_id = client_id or os.getenv("DEEPAGENT_OAUTH_CLIENT_ID", "")
        if not self.client_id:
            raise DeviceFlowError(
                "DEEPAGENT_OAUTH_CLIENT_ID is not set. Configure a GitHub OAuth App "
                "with Device Flow enabled."
            )
        self.scopes = scopes
        self._client = httpx.Client(
            base_url="https://github.com", timeout=10.0,
            headers={"Accept": "application/json"},
        )

    def request_code(self) -> DeviceCodeBundle:
        r = self._client.post(
            "/login/device/code",
            data={"client_id": self.client_id, "scope": self.scopes},
        )
        if r.status_code != 200:
            raise DeviceFlowError(f"GitHub returned {r.status_code}: {r.text}")
        d = r.json()
        return DeviceCodeBundle(
            device_code=d["device_code"],
            user_code=d["user_code"],
            verification_uri=d.get("verification_uri", "https://github.com/login/device"),
            expires_in=int(d.get("expires_in", 900)),
            interval=int(d.get("interval", 5)),
            obtained_at=time.time(),
        )

    def poll_once(self, bundle: DeviceCodeBundle) -> Optional[TokenBundle]:
        """Poll the token endpoint once.

        Returns the token on success, ``None`` if the user hasn't authorised yet.
        Raises :class:`DeviceFlowError` on terminal errors (expired, denied, …).
        """
        r = self._client.post(
            "/login/oauth/access_token",
            data={
                "client_id": self.client_id,
                "device_code": bundle.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        if r.status_code != 200:
            raise DeviceFlowError(f"GitHub returned {r.status_code}: {r.text}")
        d = r.json()
        if "access_token" in d:
            return TokenBundle(
                access_token=d["access_token"],
                token_type=d.get("token_type", "bearer"),
                scope=d.get("scope", self.scopes),
                obtained_at=time.time(),
            )
        err = d.get("error")
        if err == "authorization_pending":
            return None
        if err == "slow_down":
            # GitHub asks us to back off; bump the interval and retry later.
            bundle.interval += 5
            return None
        if err in {"expired_token", "access_denied", "unsupported_grant_type",
                   "incorrect_client_credentials", "device_flow_disabled"}:
            raise DeviceFlowError(f"OAuth terminal error: {err}")
        raise DeviceFlowError(f"Unexpected OAuth response: {d}")
