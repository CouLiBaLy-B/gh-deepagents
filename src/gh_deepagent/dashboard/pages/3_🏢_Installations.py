"""Installations page — per-installation quota inspector."""
from __future__ import annotations

import streamlit as st

from gh_deepagent.dashboard.api import APIError
from gh_deepagent.dashboard.auth_ui import (
    is_standalone, render_user_badge, require_login,
)


st.set_page_config(page_title="Installations · gh-deepagent", page_icon="🏢", layout="wide")
st.title("🏢 Installation quotas")
st.caption("Inspect quota usage for installations you have access to on GitHub.")

api, user = require_login()
render_user_badge()

if is_standalone():
    st.info(
        "Standalone mode: quota usage is tracked by the backend, which isn't "
        "reachable. Showing the raw list of installations you can access on "
        "GitHub instead."
    )
    iids = sorted(user.get("installation_ids") or [])
    if not iids:
        st.warning("You don't have any installation visible.")
    else:
        for iid in iids:
            st.markdown(f"- installation `#{iid}`")
    st.stop()


iids = sorted(user.get("installation_ids") or [])
if user.get("is_admin"):
    installation_id = st.text_input(
        "Installation ID (admin: any)",
        value=st.query_params.get("id", ""),
        placeholder="e.g. 1234567",
    )
elif iids:
    installation_id = st.selectbox(
        "Installation",
        options=iids,
        index=0,
        format_func=lambda i: f"#{i}",
    )
else:
    st.warning(
        "You don't have access to any GitHub App installation of the "
        "configured app. Install it on your org/repo first."
    )
    st.stop()

if not installation_id:
    st.info("Pick an installation above.")
    st.stop()

st.query_params["id"] = installation_id

try:
    data = api.installation_quota(installation_id)
except APIError as e:
    st.error(str(e))
    st.stop()

usage = data.get("usage", {})

cols = st.columns(3)
for col, bucket in zip(cols, ("hour", "day", "concurrent")):
    info = usage.get(bucket, {})
    used, limit = info.get("used", 0), info.get("limit", 0)
    pct = 0 if not limit else min(100, int(100 * used / limit))
    with col:
        st.markdown(f"### {bucket.capitalize()}")
        if limit == 0:
            st.caption("(unlimited — bucket disabled)")
            st.metric("Used", used)
        else:
            st.metric("Used", f"{used} / {limit}")
            st.progress(pct / 100, text=f"{pct}%")
            remaining = max(0, limit - used)
            if remaining == 0:
                st.error("Quota exhausted — webhook returns 429")
            elif pct >= 80:
                st.warning(f"{remaining} left")
            else:
                st.success(f"{remaining} left")

st.divider()
st.json(data)
