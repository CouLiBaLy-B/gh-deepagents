"""Activity / audit log page."""
from __future__ import annotations

import datetime as _dt

import streamlit as st

from gh_deepagent.dashboard.api import APIError
from gh_deepagent.dashboard.auth_ui import render_user_badge, require_login


st.set_page_config(page_title="Activity · gh-deepagent", page_icon="📜", layout="wide")
st.title("📜 Activity log")
st.caption("Audit trail of state-changing operations: job creation, role changes, "
           "DLQ requeues, cost resets. Newest first.")

api, user = require_login()
render_user_badge()

scope_options = ["My installations"]
if user.get("is_admin"):
    scope_options.append("Global (admin)")

scope = st.radio("Scope", scope_options, horizontal=True)

limit = st.slider("Show last N events", 50, 1000, 200, step=50)

# Choose data source
if scope == "Global (admin)":
    try:
        events = api.audit_global(limit=limit)
    except APIError as e:
        st.error(str(e))
        st.stop()
else:
    try:
        installations = api.installations()
    except APIError as e:
        st.error(str(e))
        st.stop()
    if not installations:
        st.info("You don't have access to any installation.")
        st.stop()

    iid_choices = {f"#{i['installation_id']}": i["installation_id"] for i in installations}
    pick = st.selectbox("Installation", list(iid_choices.keys()))
    iid = iid_choices[pick]
    try:
        events = api.installation_audit(iid, limit=limit)
    except APIError as e:
        st.error(str(e))
        st.stop()

# Filters
c1, c2 = st.columns(2)
action_filter = c1.text_input("Filter by action (substring)", value="")
actor_filter = c2.text_input("Filter by actor (substring)", value="")

filtered = [
    e for e in events
    if (not action_filter or action_filter.lower() in (e.get("action") or "").lower())
    and (not actor_filter or actor_filter.lower() in (e.get("actor") or "").lower())
]

if not filtered:
    st.info("No matching events.")
    st.stop()

import pandas as _pd

rows = []
for e in filtered:
    ts = e.get("timestamp")
    rows.append({
        "When": _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                if ts else "—",
        "Action": e.get("action"),
        "Actor": e.get("actor"),
        "Via": e.get("via"),
        "Target": e.get("target") or "",
        "Installation": e.get("installation_id"),
        "Metadata": ", ".join(f"{k}={v}" for k, v in (e.get("metadata") or {}).items())[:200],
    })
df = _pd.DataFrame(rows)
st.dataframe(df, use_container_width=True, hide_index=True)

# Counters
st.divider()
from collections import Counter
top_actions = Counter(e.get("action") for e in filtered).most_common(8)
top_actors = Counter(e.get("actor") for e in filtered).most_common(8)
c1, c2 = st.columns(2)
with c1:
    st.subheader("Top actions")
    if top_actions:
        st.bar_chart({a: n for a, n in top_actions}, horizontal=True)
with c2:
    st.subheader("Top actors")
    if top_actors:
        st.bar_chart({a: n for a, n in top_actors}, horizontal=True)
