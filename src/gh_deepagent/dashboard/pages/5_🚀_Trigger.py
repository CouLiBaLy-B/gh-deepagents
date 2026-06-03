"""Trigger page — kick off a job locally (CLI shortcut).

This page runs the agent **in-process** via the runner, NOT through the queue.
Useful for one-off operator interventions; for production use, prefer GitHub
labels / comments which go through the queue.
"""
from __future__ import annotations

import os

import streamlit as st

from gh_deepagent.dashboard.auth_ui import render_user_badge, require_login


st.set_page_config(page_title="Trigger · gh-deepagent", page_icon="🚀", layout="wide")
st.title("🚀 Trigger a job")
st.caption(
    "Run the agent directly from this dashboard. This bypasses the Redis queue "
    "(useful for ad-hoc operator work). For end-user workflows, prefer the "
    "GitHub label / `/deepagent` comment path."
)

_api, user = require_login()
render_user_badge()
if not user.get("is_admin"):
    st.error(
        "Triggering jobs through the dashboard is restricted to admins. "
        "End users should use GitHub labels or `/deepagent` comments instead."
    )
    st.stop()

st.warning(
    "⚠️ This page runs the agent inside the Streamlit process. It will block "
    "the UI for the duration of the run. For long jobs, use the queue."
)

tab_fix, tab_evolve, tab_review = st.tabs(["Fix issue", "Evolve repo", "Review PR"])

with tab_fix:
    issue_url = st.text_input("Issue URL",
                              placeholder="https://github.com/org/repo/issues/42")
    dry = st.checkbox("Dry-run (no PR)", key="dry-fix")
    if st.button("Run fix_issue", type="primary", disabled=not issue_url):
        with st.spinner("Agent running…"):
            try:
                from gh_deepagent.runner import fix_issue
                res = fix_issue(issue_url, dry_run=dry)
                if res.pr_url:
                    st.success(f"✅ PR opened: {res.pr_url}")
                st.markdown(res.summary or "(no summary)")
                if res.diff:
                    with st.expander("Diff"):
                        st.code(res.diff, language="diff")
            except Exception as e:
                st.error(f"Failed: {e}")

with tab_evolve:
    repo = st.text_input("Repo (owner/name)", value=os.getenv("DEEPAGENT_DEFAULT_REPO", ""),
                         key="evolve-repo")
    instruction = st.text_area("Instruction", placeholder="What should the agent change?",
                               height=120, key="evolve-instr")
    dry2 = st.checkbox("Dry-run (no PR)", key="dry-evolve")
    if st.button("Run evolve_code", type="primary",
                 disabled=not (repo and instruction)):
        with st.spinner("Agent running…"):
            try:
                from gh_deepagent.runner import evolve_code
                res = evolve_code(repo, instruction, dry_run=dry2)
                if res.pr_url:
                    st.success(f"✅ PR opened: {res.pr_url}")
                st.markdown(res.summary or "(no summary)")
                if res.diff:
                    with st.expander("Diff"):
                        st.code(res.diff, language="diff")
            except Exception as e:
                st.error(f"Failed: {e}")

with tab_review:
    repo3 = st.text_input("Repo (owner/name)", value=os.getenv("DEEPAGENT_DEFAULT_REPO", ""),
                          key="review-repo")
    pr_n = st.number_input("PR number", min_value=1, step=1, key="review-pr")
    if st.button("Run review_pr", type="primary",
                 disabled=not (repo3 and pr_n)):
        with st.spinner("Reviewing…"):
            try:
                from gh_deepagent.runner import review_pr
                res = review_pr(repo3, int(pr_n))
                if res.pr_url:
                    st.success(f"Posted review on {res.pr_url}")
                st.markdown(res.summary or "(no summary)")
            except Exception as e:
                st.error(f"Failed: {e}")
