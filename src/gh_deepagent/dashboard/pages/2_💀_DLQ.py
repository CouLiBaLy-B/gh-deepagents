"""DLQ page — review dead jobs and requeue."""
from __future__ import annotations

import streamlit as st

from gh_deepagent.dashboard.api import APIError
from gh_deepagent.dashboard.auth_ui import (
    render_user_badge, require_backend, require_login,
)


st.set_page_config(page_title="DLQ · gh-deepagent", page_icon="💀", layout="wide")
st.title("💀 Dead-Letter Queue")
st.caption("Jobs that exhausted all retries. Requeue once you've fixed the root cause. **Admin-only.**")

api, user = require_login()
render_user_badge()
require_backend("DLQ")
if not user.get("is_admin"):
    st.error("You need admin privileges to view the DLQ.")
    st.stop()

limit = st.slider("Show up to", 10, 200, 50, step=10)

try:
    rows = api.dlq(limit=limit)
except APIError as e:
    st.error(f"Failed to fetch DLQ: {e}")
    st.stop()

if not rows:
    st.success("🎉 DLQ is empty.")
    st.stop()

st.warning(f"{len(rows)} dead job(s)")

# Bulk requeue
if st.button(f"♻️ Requeue all {len(rows)}", type="primary"):
    ok = 0
    fail = 0
    for r in rows:
        try:
            api.requeue(r["id"])
            ok += 1
        except APIError:
            fail += 1
    st.success(f"Requeued {ok} (failed {fail}). Refreshing…")
    st.rerun()

st.divider()

for r in rows:
    with st.container(border=True):
        c1, c2, c3 = st.columns([2, 4, 1])
        with c1:
            st.markdown(f"**{r['event']}**")
            st.caption(r["id"])
        with c2:
            st.write(f"**Repo:** `{r['repo']}` · **Attempts:** {r['attempts']}")
            st.code(r.get("error") or "(no error message)", language="text")
        with c3:
            if st.button("♻️ Requeue", key=f"rq-{r['id']}"):
                try:
                    api.requeue(r["id"])
                    st.success("Requeued.")
                    st.rerun()
                except APIError as e:
                    st.error(str(e))
            st.markdown(f"[Inspect](Jobs?id={r['id']})")
