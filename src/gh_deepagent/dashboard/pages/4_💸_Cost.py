"""Cost page — per-installation spend, scoped to whatever the user can see."""
from __future__ import annotations

import streamlit as st

from gh_deepagent.dashboard.api import APIError
from gh_deepagent.dashboard.auth_ui import (
    render_user_badge, require_backend, require_login,
)


st.set_page_config(page_title="Cost · gh-deepagent", page_icon="💸", layout="wide")
st.title("💸 LLM cost & token usage")

api, user = require_login()
render_user_badge()
require_backend("LLM cost breakdown")

try:
    installations = api.installations()
except APIError as e:
    st.error(str(e))
    st.stop()

if not installations:
    st.info("You don't have access to any installation yet.")
    st.stop()

st.caption("Select an installation to see its breakdown. Cost is attributed to "
           "the installation that triggered the job (hosted models only — local "
           "Ollama is counted at $0).")

# ---- selector
options = {f"#{i['installation_id']} ({i['role'] or '—'})": i["installation_id"]
           for i in installations}
choice = st.selectbox("Installation", list(options.keys()))
iid = options[choice]

try:
    data = api.installation_cost(iid)
except APIError as e:
    st.error(str(e))
    st.stop()

total_usd = data.get("total_usd", 0.0)
models = data.get("models", {})

c1, c2, c3 = st.columns(3)
c1.metric("Total spend", f"${total_usd:.4f}")
c2.metric("Models used", len(models))
total_tokens = sum(m.get("input_tokens", 0) + m.get("output_tokens", 0) for m in models.values())
c3.metric("Total tokens", f"{total_tokens:,}".replace(",", " "))

st.divider()

if not models:
    st.info("No LLM activity recorded for this installation yet.")
    st.stop()

import pandas as _pd
rows = []
for model_key, m in models.items():
    rows.append({
        "provider:model": model_key,
        "calls": "—",   # not tracked per-tenant (in global metrics only)
        "in tokens": m.get("input_tokens", 0),
        "out tokens": m.get("output_tokens", 0),
        "USD": round(m.get("usd", 0.0), 6),
    })
df = _pd.DataFrame(rows).sort_values("USD", ascending=False)
st.dataframe(df, use_container_width=True, hide_index=True,
             column_config={
                 "USD": st.column_config.NumberColumn(format="$%.6f"),
                 "in tokens": st.column_config.NumberColumn(format="%d"),
                 "out tokens": st.column_config.NumberColumn(format="%d"),
             })

st.divider()

# ---- charts
c1, c2 = st.columns(2)
with c1:
    st.subheader("Tokens by model")
    by_model_tokens = {r["provider:model"]: r["in tokens"] + r["out tokens"] for r in rows}
    st.bar_chart(by_model_tokens, horizontal=True)
with c2:
    st.subheader("Cost share")
    by_model_cost = {r["provider:model"]: r["USD"] for r in rows if r["USD"] > 0}
    if by_model_cost:
        st.bar_chart(by_model_cost, horizontal=True)
    else:
        st.caption("No paid-for calls.")

st.divider()

# ---- admin: reset
roles = api.list_roles(iid) if user.get("is_admin") or _is_admin_on(api, iid) else None
if user.get("is_admin") or (roles and roles.get("roles", {}).get(user["login"]) == "admin"):
    with st.expander("⚠️ Reset cost counter (admin only)"):
        st.caption("Use to start a new billing period. This is irreversible.")
        if st.button("Reset", type="secondary"):
            try:
                api.reset_installation_cost(iid)
                st.success("Counter reset.")
                st.rerun()
            except APIError as e:
                st.error(str(e))


def _is_admin_on(api, iid):  # pragma: no cover — UI guard
    try:
        r = api.list_roles(iid).get("roles", {})
        return r.get(st.session_state["deepagent.user"]["login"]) == "admin"
    except Exception:
        return False
