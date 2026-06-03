"""Jobs page — inspect a specific job + tail its logs."""
from __future__ import annotations

import json

import streamlit as st

from gh_deepagent.dashboard.api import APIError
from gh_deepagent.dashboard.auth_ui import render_user_badge, require_login


st.set_page_config(page_title="Jobs · gh-deepagent", page_icon="📋", layout="wide")
st.title("📋 Job inspector")

api, _user = require_login()
render_user_badge()

# ----- Look up a job -----
job_id = st.text_input("Job ID", value=st.query_params.get("id", ""),
                       placeholder="paste a UUID returned by POST /webhook")

if not job_id:
    st.info(
        "Enter a job ID above. You can also navigate here via "
        "`?id=<uuid>` in the URL."
    )
    st.stop()

st.query_params["id"] = job_id

try:
    job = api.job(job_id)
except APIError as e:
    st.error(f"Job lookup failed: {e}")
    st.stop()


# ----- Header: status + metadata -----
status = job.get("status", "?")
icon = {"pending": "⏳", "running": "🏃", "succeeded": "✅",
        "failed": "❌", "dead": "💀", "skipped": "⏭️"}.get(status, "❓")
left, right = st.columns([3, 1])
with left:
    st.subheader(f"{icon} {status.upper()}")
    st.caption(f"`{job_id}`")
with right:
    if status in {"pending", "running"}:
        st.info("Job is still in flight")
    elif status == "dead":
        if st.button("♻️ Requeue from DLQ", type="primary"):
            try:
                api.requeue(job_id)
                st.success("Requeued; refresh in a few seconds.")
            except APIError as e:
                st.error(str(e))

# ----- Two columns: metadata + result/error -----
col_meta, col_result = st.columns(2)
with col_meta:
    st.markdown("**Metadata**")
    st.write(
        {
            "event": job.get("event"),
            "repo": job.get("repo_full_name"),
            "installation_id": job.get("installation_id"),
            "delivery_id": job.get("delivery_id"),
            "attempts": job.get("attempts"),
            "created_at": job.get("created_at"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
        }
    )
with col_result:
    st.markdown("**Result**")
    if job.get("error"):
        st.error(job["error"])
    elif job.get("result"):
        st.json(job["result"])
    else:
        st.caption("(no result yet)")

# ----- Logs / live tail -----
st.divider()
st.subheader("Logs")

mode = st.radio("Mode", ["Snapshot", "Live tail (SSE)"], horizontal=True,
                index=0 if status in {"succeeded", "failed", "dead"} else 1)

if mode == "Snapshot":
    tail = st.slider("Tail size", 10, 500, 200, step=10)
    try:
        lines = api.job_logs(job_id, tail=tail)
    except APIError as e:
        st.error(str(e))
        st.stop()
    if not lines:
        st.caption("(no logs)")
    else:
        st.code("\n".join(lines), language="text", line_numbers=True)
else:
    st.caption("Streaming live — connection closes automatically when the job finishes.")
    placeholder = st.empty()
    status_box = st.empty()
    buffer: list[str] = []
    try:
        for event, data in api.stream_job(job_id, replay=True):
            if event == "log":
                buffer.append(data)
                # cap displayed buffer to avoid bloating the page
                if len(buffer) > 1000:
                    buffer = buffer[-1000:]
                placeholder.code("\n".join(buffer), language="text", line_numbers=True)
            elif event == "status":
                try:
                    s = json.loads(data)
                    status_box.info(f"Status: **{s.get('status')}** — {s.get('error') or 'OK'}")
                    if s.get("status") in {"succeeded", "failed", "dead"}:
                        break
                except Exception:
                    status_box.code(data)
    except APIError as e:
        st.error(str(e))
    st.success("Stream closed.")
