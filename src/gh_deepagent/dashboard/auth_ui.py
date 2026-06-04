"""Shared Streamlit helpers for auth + scoped API instances.

Every page calls :func:`require_login` at the top. It either returns a
fully-built :class:`WebhookAPI` bound to the user's token, or stops execution
with a login prompt.

Two auth modes:

1. **Backend mode** — the webhook URL is reachable. The token is validated
   *against the backend* (`GET /me`), and pages query backend-scoped data.

2. **Standalone mode** — the webhook URL is unreachable (typical for a dashboard
   Space deployed without a paired backend). The token is validated *directly
   against GitHub* (`GET https://api.github.com/user`). Pages that don't need
   the backend still work; pages that do show a friendly banner.

This makes the dashboard Space usable on its own as a demo / preview without
forcing the user to spin up a backend first.
"""
from __future__ import annotations

import time
from typing import Optional

import httpx
import streamlit as st

from gh_deepagent.dashboard.api import APIError, WebhookAPI
from gh_deepagent.dashboard.oauth import DeviceFlowError, GitHubDeviceFlow


SESSION_TOKEN_KEY = "deepagent.token"
SESSION_USER_KEY = "deepagent.user"
SESSION_DEVICE_KEY = "deepagent.device_bundle"
SESSION_STANDALONE_KEY = "deepagent.standalone"


# ---------------------------------------------------------------- API helpers

def _api(token: Optional[str] = None) -> WebhookAPI:
    return WebhookAPI(
        base_url=st.session_state.get("api_base"),
        token=token,
    )


def is_standalone() -> bool:
    """True iff we authenticated against GitHub directly (no backend)."""
    return bool(st.session_state.get(SESSION_STANDALONE_KEY))


def require_backend(feature: str = "this page") -> None:
    """Pages that strictly need a backend call this. Renders a friendly stop banner."""
    if is_standalone():
        st.warning(
            f"🛑 **{feature}** needs a reachable backend webhook to work. "
            "You're currently in **standalone mode** (signed in against GitHub "
            "directly). Configure `DEEPAGENT_API_URL` in the sidebar to point "
            "at your webhook, or deploy the all-in-one demo Space."
        )
        st.stop()


# ---------------------------------------------------------------- token validation

def _validate_against_backend(token: str) -> Optional[dict]:
    """Try `GET /me` on the configured webhook. Returns the user dict on
    success, ``None`` if rejected, raises :class:`ConnectionError` if the backend
    is unreachable so the caller can fall back to GitHub.
    """
    api = _api(token=token)
    try:
        return api.whoami()
    except APIError as e:
        # status=0 means transport error (refused, DNS, timeout).
        if e.status == 0:
            raise ConnectionError(e.body or "connection failed")
        st.error(f"Token rejected by backend ({e.status}): {e.body[:200]}")
        return None


def _validate_against_github(token: str) -> Optional[dict]:
    """Direct GitHub validation. Builds the same user dict shape the backend would."""
    try:
        with httpx.Client(timeout=8.0, headers={"Accept": "application/vnd.github+json"}) as h:
            r = h.get("https://api.github.com/user",
                      headers={"Authorization": f"Bearer {token}"})
            if r.status_code != 200:
                st.error(f"GitHub rejected the token: HTTP {r.status_code}")
                return None
            login = (r.json().get("login") or "").lower()

            iids: list[int] = []
            page = 1
            while page <= 10:
                ri = h.get("https://api.github.com/user/installations",
                           headers={"Authorization": f"Bearer {token}"},
                           params={"per_page": 100, "page": page})
                if ri.status_code != 200:
                    break
                data = ri.json()
                for inst in data.get("installations", []):
                    iids.append(int(inst["id"]))
                if len(data.get("installations", [])) < 100:
                    break
                page += 1
        return {"login": login, "is_admin": False, "via": "github",
                "installation_ids": iids}
    except httpx.HTTPError as e:
        st.error(f"Couldn't reach GitHub: {e}")
        return None


def _persist_user(token: str) -> Optional[dict]:
    """Validate the token, prefer backend, fall back to direct GitHub."""
    info = None
    standalone = False
    try:
        info = _validate_against_backend(token)
    except ConnectionError:
        st.warning(
            "ℹ️ Backend webhook unreachable — signing you in against GitHub "
            "directly. Backend-scoped pages (DLQ, full Cost) will be limited."
        )
        info = _validate_against_github(token)
        standalone = info is not None
    if not info:
        return None
    st.session_state[SESSION_TOKEN_KEY] = token
    st.session_state[SESSION_USER_KEY] = info
    st.session_state[SESSION_STANDALONE_KEY] = standalone
    return info


# ---------------------------------------------------------------- guards

def require_login() -> tuple[WebhookAPI, dict]:
    """If logged in, returns (api, user_info). Else renders the login UI and stops."""
    token = st.session_state.get(SESSION_TOKEN_KEY)
    user = st.session_state.get(SESSION_USER_KEY)
    if token and user:
        return _api(token=token), user

    _render_login()
    st.stop()


def logout() -> None:
    for key in (SESSION_TOKEN_KEY, SESSION_USER_KEY, SESSION_DEVICE_KEY,
                SESSION_STANDALONE_KEY):
        st.session_state.pop(key, None)


def render_user_badge() -> None:
    """Sidebar widget: logged-in user + installation list + logout button."""
    user = st.session_state.get(SESSION_USER_KEY)
    if not user:
        return
    with st.sidebar:
        st.divider()
        st.markdown(f"👤 **{user['login']}**")
        mode = "standalone" if is_standalone() else "backend-scoped"
        st.caption(f"via {user['via']} · {mode}"
                   + (" · admin" if user.get('is_admin') else ""))
        iids = user.get("installation_ids") or []
        if iids:
            st.caption(f"{len(iids)} installation(s): " + ", ".join(map(str, iids[:5])) +
                       ("…" if len(iids) > 5 else ""))
        if st.button("Sign out", use_container_width=True):
            logout()
            st.rerun()


# ============================================================ login UI

def _render_login() -> None:
    st.title("🔐 Sign in to gh-deepagent")
    st.caption("Authenticate to view jobs scoped to your GitHub installations.")

    _maybe_backend_warning()

    tab_oauth, tab_token = st.tabs(["GitHub (Device Flow)", "Paste token"])

    # ---------------- OAuth Device Flow ----------------
    with tab_oauth:
        try:
            flow = GitHubDeviceFlow()
        except DeviceFlowError as e:
            st.info(
                "🔧 **GitHub OAuth Device Flow isn't configured yet.**\n\n"
                "To enable one-click GitHub sign-in:\n\n"
                "1. Create a GitHub OAuth App with *Device Flow* enabled — "
                "[go here](https://github.com/settings/developers) → "
                "**OAuth Apps** → **New OAuth App**.\n"
                "2. Tick **Enable Device Flow** at the bottom of the form.\n"
                "3. Copy the **Client ID** "
                "(format `Iv1.xxxxxxxx` or `Ov23liXXXXXXX`, **not** the number in the URL).\n"
                "4. In your Space *Settings → Variables and secrets* → Variables, "
                "set `DEEPAGENT_OAUTH_CLIENT_ID` to that value.\n\n"
                "👉 **In the meantime, use the \"Paste token\" tab** with a "
                "GitHub Personal Access Token (`repo` scope)."
            )
            st.caption(f"_Raw error: {e}_")
            return

        bundle = st.session_state.get(SESSION_DEVICE_KEY)
        if bundle and bundle.expired():
            st.session_state.pop(SESSION_DEVICE_KEY, None)
            bundle = None

        if not bundle:
            if st.button("🔑 Sign in with GitHub", type="primary",
                         use_container_width=True):
                try:
                    bundle = flow.request_code()
                    st.session_state[SESSION_DEVICE_KEY] = bundle
                    st.rerun()
                except DeviceFlowError as e:
                    _render_device_flow_error(e)
            st.caption("Opens a one-time code you'll paste on github.com/login/device.")
        else:
            c1, c2 = st.columns([2, 3])
            with c1:
                st.markdown("**Your one-time code**")
                st.code(bundle.user_code, language="text")
                st.markdown(f"[👉 Open {bundle.verification_uri}]({bundle.verification_uri})")
                st.caption(
                    f"Expires in {int(bundle.expires_at - time.time())}s — "
                    f"polling every {bundle.interval}s."
                )
            with c2:
                st.info(
                    "1. Click the link above\n"
                    "2. Enter the code\n"
                    "3. Approve the requested scopes\n"
                    "4. Come back here — sign-in completes automatically."
                )

            try:
                tok = flow.poll_once(bundle)
            except DeviceFlowError as e:
                _render_device_flow_error(e)
                st.session_state.pop(SESSION_DEVICE_KEY, None)
                if st.button("Try again"):
                    st.rerun()
                return

            if tok:
                user = _persist_user(tok.access_token)
                if user:
                    st.session_state.pop(SESSION_DEVICE_KEY, None)
                    st.success(f"Signed in as **{user['login']}** ✅")
                    time.sleep(0.3)
                    st.rerun()
            else:
                with st.spinner("Waiting for authorisation…"):
                    time.sleep(max(2, bundle.interval))
                st.rerun()

    # ---------------- Paste token (PAT or admin) ----------------
    with tab_token:
        st.caption(
            "Paste a GitHub personal access token (classic, with `repo` scope) "
            "OR the `DEEPAGENT_ADMIN_TOKEN` value. If a backend is reachable we "
            "validate against it; otherwise we validate directly against GitHub."
        )
        token = st.text_input("Token", type="password", key="token-paste")
        if st.button("Sign in", disabled=not token):
            user = _persist_user(token)
            if user:
                st.success(f"Signed in as **{user['login']}** ✅")
                time.sleep(0.3)
                st.rerun()


# ---------------------------------------------------------------- diagnostics

def _maybe_backend_warning() -> None:
    """Probe the configured webhook /healthz once per session and warn if down."""
    if st.session_state.get("_backend_probed"):
        return
    st.session_state["_backend_probed"] = True
    api = _api()
    try:
        api.healthz()
        st.session_state["_backend_alive"] = True
    except APIError as e:
        st.session_state["_backend_alive"] = False
        st.warning(
            f"⚠️ Backend webhook at `{api.base_url}` is unreachable "
            f"(`{str(e)[:120]}`). You can still sign in to browse the UI; "
            "data pages will use GitHub directly where possible."
        )


def _render_device_flow_error(e: DeviceFlowError) -> None:
    """Turn raw GitHub errors into actionable advice."""
    msg = str(e)
    st.error(msg)
    if "404" in msg or "Not Found" in msg:
        st.info(
            "💡 **HTTP 404 from GitHub usually means the OAuth `client_id` is wrong.**\n\n"
            "- The Client ID looks like `Iv1.xxxxxxxx` or `Ov23liXXXXXXX`, "
            "NOT the number in the URL (`/settings/applications/<id>` shows "
            "the *App ID*, which is different).\n"
            "- Find it on the OAuth App page (https://github.com/settings/developers → "
            "OAuth Apps → your app), under **Client ID**.\n"
            "- Update `DEEPAGENT_OAUTH_CLIENT_ID` in your Space settings, then "
            "wait ~10s for the Space to restart."
        )
    elif "device_flow_disabled" in msg:
        st.info(
            "💡 Your OAuth App doesn't have *Enable Device Flow* checked. "
            "Open the app on github.com/settings/developers and tick it, then retry."
        )
