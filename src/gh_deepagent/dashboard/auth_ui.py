"""Shared Streamlit helpers for auth + scoped API instances.

Every page calls :func:`require_login` at the top. It either returns a
fully-built :class:`WebhookAPI` bound to the user's token, or stops execution
with a login prompt.
"""
from __future__ import annotations

import time
from typing import Optional

import streamlit as st

from gh_deepagent.dashboard.api import APIError, WebhookAPI
from gh_deepagent.dashboard.oauth import DeviceFlowError, GitHubDeviceFlow


SESSION_TOKEN_KEY = "deepagent.token"
SESSION_USER_KEY = "deepagent.user"
SESSION_DEVICE_KEY = "deepagent.device_bundle"


def _api(token: Optional[str] = None) -> WebhookAPI:
    return WebhookAPI(
        base_url=st.session_state.get("api_base"),
        token=token,
    )


def _persist_user(token: str) -> Optional[dict]:
    """Validate the token against the webhook and stash the user info."""
    api = _api(token=token)
    try:
        info = api.whoami()
    except APIError as e:
        st.error(f"Token rejected by server: {e}")
        return None
    st.session_state[SESSION_TOKEN_KEY] = token
    st.session_state[SESSION_USER_KEY] = info
    return info


def require_login() -> tuple[WebhookAPI, dict]:
    """If logged in, returns (api, user_info). Else renders the login UI and stops."""
    token = st.session_state.get(SESSION_TOKEN_KEY)
    user = st.session_state.get(SESSION_USER_KEY)
    if token and user:
        return _api(token=token), user

    _render_login()
    st.stop()


def logout() -> None:
    for key in (SESSION_TOKEN_KEY, SESSION_USER_KEY, SESSION_DEVICE_KEY):
        st.session_state.pop(key, None)


def render_user_badge() -> None:
    """Sidebar widget: logged-in user + installation list + logout button."""
    user = st.session_state.get(SESSION_USER_KEY)
    if not user:
        return
    with st.sidebar:
        st.divider()
        st.markdown(f"👤 **{user['login']}**")
        st.caption(f"via {user['via']}{' · admin' if user.get('is_admin') else ''}")
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

    tab_oauth, tab_token = st.tabs(["GitHub (Device Flow)", "Paste token"])

    # ---------------- OAuth Device Flow ----------------
    with tab_oauth:
        try:
            flow = GitHubDeviceFlow()
        except DeviceFlowError as e:
            st.info(
                "🔧 **GitHub OAuth Device Flow isn\'t configured yet.**\n\n"
                "To enable one-click GitHub sign-in:\n"
                "1. Create a GitHub OAuth App with *Device Flow* enabled "
                "([instructions](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app))\n"
                "2. Set `DEEPAGENT_OAUTH_CLIENT_ID` in your Space settings → "
                "Variables and secrets.\n\n"
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
            if st.button("🔑 Sign in with GitHub", type="primary", use_container_width=True):
                try:
                    bundle = flow.request_code()
                    st.session_state[SESSION_DEVICE_KEY] = bundle
                    st.rerun()
                except DeviceFlowError as e:
                    st.error(str(e))
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

            # Poll once per rerun (cheap; Streamlit will rerun on the timer below).
            try:
                tok = flow.poll_once(bundle)
            except DeviceFlowError as e:
                st.error(str(e))
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
                # Wait then rerun. We use a placeholder so the UI doesn't freeze.
                with st.spinner("Waiting for authorisation…"):
                    time.sleep(max(2, bundle.interval))
                st.rerun()

    # ---------------- Paste token (PAT or admin) ----------------
    with tab_token:
        st.caption(
            "Paste a GitHub personal access token (classic, with `repo` scope) "
            "OR the `DEEPAGENT_ADMIN_TOKEN` value. We validate it against the server."
        )
        token = st.text_input("Token", type="password", key="token-paste")
        if st.button("Sign in", disabled=not token):
            user = _persist_user(token)
            if user:
                st.success(f"Signed in as **{user['login']}** ✅")
                time.sleep(0.3)
                st.rerun()
