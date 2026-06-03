"""Streamlit entrypoint — multi-page, multi-tenant admin dashboard."""
from __future__ import annotations

import os
import time

import streamlit as st

from .api import APIError, parse_prometheus, sum_by, total
from .auth_ui import render_user_badge, require_login


st.set_page_config(
    page_title="gh-deepagent",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------- sidebar (drawn before login so the user can change the API URL) ----------
with st.sidebar:
    st.title("🤖 gh-deepagent")
    st.caption("Admin dashboard")
    base = st.text_input(
        "API base URL",
        value=st.session_state.get("api_base") or os.getenv("DEEPAGENT_API_URL", "http://localhost:8080"),
        key="api_base_input",
        help="The gh-deepagent webhook server.",
    )
    if base != st.session_state.get("api_base"):
        st.session_state["api_base"] = base
    st.session_state.setdefault("autorefresh", True)
    st.session_state["autorefresh"] = st.toggle(
        "Auto-refresh (10s)", value=st.session_state["autorefresh"]
    )

# ---------- login gate ----------
api, user = require_login()

# Sidebar widgets that depend on being logged in.
with st.sidebar:
    st.divider()
    try:
        h = api.healthz()
        if h.get("status") == "ok":
            st.success(f"✅ Healthy · queue {h.get('queue_depth', '?')} · DLQ {h.get('dead_letter', '?')}")
        else:
            st.warning(f"⚠️ Degraded · {h}")
    except APIError as e:
        st.error(f"❌ Unreachable: {e}")
render_user_badge()


# ---------- HOME page ----------
st.title("Overview")
st.caption(f"Signed in as **{user['login']}** — "
           f"{'admin (sees all)' if user.get('is_admin') else f'{len(user.get(\"installation_ids\") or [])} installation(s)'}.")

# For admins, show the global Prometheus state. For users, build KPIs from
# their own jobs (which are already scoped server-side).
if user.get("is_admin"):
    try:
        metrics = parse_prometheus(api.metrics_raw())
    except APIError as e:
        st.error(f"Failed to fetch /metrics: {e}")
        st.stop()

    def _val(name: str) -> float:
        return total(metrics.get(name, []))

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Queue depth", int(_val("deepagent_queue_depth")))
    dlq = int(_val("deepagent_dlq_size"))
    k2.metric("DLQ", dlq, delta="⚠️" if dlq else None,
              delta_color="inverse" if dlq else "off")
    k3.metric("In-progress", int(_val("deepagent_jobs_in_progress")))
    k4.metric("Jobs total", int(_val("deepagent_jobs_total")))
    k5.metric("LLM spend (USD)", f"${_val('deepagent_llm_cost_usd_total'):.2f}")

    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Jobs by status")
        by_status = sum_by(metrics.get("deepagent_jobs_total", []), "status")
        if by_status:
            st.bar_chart(by_status, horizontal=True)
        else:
            st.info("No job activity yet.")
    with c2:
        st.subheader("Sub-agent invocations")
        by_sub = sum_by(metrics.get("deepagent_subagent_calls_total", []), "subagent")
        if by_sub:
            st.bar_chart(by_sub, horizontal=True)
        else:
            st.info("No sub-agent calls yet.")

else:
    # Tenant view — build KPIs from /jobs (scoped server-side).
    try:
        my_jobs = api.list_jobs(limit_per_install=50)
    except APIError as e:
        st.error(f"Failed to list jobs: {e}")
        st.stop()

    from collections import Counter
    statuses = Counter(j["status"] for j in my_jobs)
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Recent jobs", len(my_jobs))
    k2.metric("Running", statuses.get("running", 0) + statuses.get("pending", 0))
    k3.metric("Failed", statuses.get("failed", 0) + statuses.get("dead", 0))
    k4.metric("Succeeded", statuses.get("succeeded", 0))

    st.divider()
    st.subheader("Your recent jobs")
    if not my_jobs:
        st.info("No jobs yet for your installations. Trigger one by labeling an "
                "issue with `deepagent` or commenting `/deepagent <instruction>`.")
    else:
        import pandas as _pd
        df = _pd.DataFrame([{
            "When": _fmt_ts(j.get("created_at")),
            "Repo": j["repo"],
            "Event": j["event"],
            "Status": j["status"],
            "ID": j["id"],
        } for j in my_jobs[:50]])
        st.dataframe(df, use_container_width=True, hide_index=True,
                     column_config={"ID": st.column_config.LinkColumn(
                         "Inspect", display_text="View",
                         help="Open job inspector",
                     )} if False else None)
        st.caption("Open a job by copying its ID into the **Jobs** page.")


if st.session_state.get("autorefresh"):
    time.sleep(10)
    st.rerun()


def _fmt_ts(ts):
    if not ts:
        return "—"
    import datetime as _dt
    return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
