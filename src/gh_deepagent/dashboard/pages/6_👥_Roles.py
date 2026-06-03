"""Role management — admin-on-installation can grant/revoke roles."""
from __future__ import annotations

import streamlit as st

from gh_deepagent.dashboard.api import APIError
from gh_deepagent.dashboard.auth_ui import render_user_badge, require_login


st.set_page_config(page_title="Roles · gh-deepagent", page_icon="👥", layout="wide")
st.title("👥 Role management")
st.caption("Grant **viewer** / **operator** / **admin** roles on a per-installation basis. "
           "You need the *admin* role on the installation (or be a global admin) to change roles.")

api, user = require_login()
render_user_badge()

try:
    installations = api.installations()
except APIError as e:
    st.error(str(e))
    st.stop()

# Filter to installations where we can manage roles.
manageable = [i for i in installations
              if user.get("is_admin") or i.get("role") == "admin"]
if not manageable:
    st.warning(
        "You aren't an *admin* on any installation. Ask the installation owner "
        "to grant you the admin role on the relevant installation."
    )
    if installations:
        st.caption("Installations you can read:")
        st.json([{"installation_id": i["installation_id"], "role": i["role"]} for i in installations])
    st.stop()

options = {f"#{i['installation_id']} (you: {i['role'] or '—'})": i["installation_id"]
           for i in manageable}
choice = st.selectbox("Installation", list(options.keys()))
iid = options[choice]

# ---- list current roles
try:
    data = api.list_roles(iid)
except APIError as e:
    st.error(str(e))
    st.stop()

roles = data.get("roles", {})

st.subheader(f"Roles on installation #{iid}")
st.caption("Logins without an explicit role default to **viewer** if they have "
           "GitHub App access.")
if not roles:
    st.info("No explicit role assignments yet.")
else:
    for login, role in sorted(roles.items()):
        c1, c2, c3, c4 = st.columns([3, 2, 2, 1])
        c1.markdown(f"**{login}**")
        c2.markdown(f"`{role}`")
        with c3:
            new = st.selectbox(
                "Change to",
                ["viewer", "operator", "admin"],
                index=["viewer", "operator", "admin"].index(role),
                key=f"change-{iid}-{login}",
                label_visibility="collapsed",
            )
            if new != role:
                if st.button("Save", key=f"save-{iid}-{login}"):
                    try:
                        api.set_role(iid, login, new)
                        st.success(f"{login} → {new}")
                        st.rerun()
                    except APIError as e:
                        st.error(str(e))
        with c4:
            if st.button("🗑️", key=f"del-{iid}-{login}", help="Revoke explicit role"):
                try:
                    api.remove_role(iid, login)
                    st.success(f"Revoked {login}")
                    st.rerun()
                except APIError as e:
                    st.error(str(e))

st.divider()

# ---- grant new
st.subheader("Grant a role")
c1, c2, c3 = st.columns([3, 2, 1])
new_login = c1.text_input("GitHub login", placeholder="e.g. alice")
new_role = c2.selectbox("Role", ["viewer", "operator", "admin"], index=1)
if c3.button("Grant", type="primary", disabled=not new_login):
    try:
        api.set_role(iid, new_login, new_role)
        st.success(f"Granted {new_role} to {new_login}.")
        st.rerun()
    except APIError as e:
        st.error(str(e))

st.divider()
st.caption(
    "ℹ️ Roles are stored in Redis (`deepagent:role:<installation>`). A user "
    "must still have access to the GitHub App installation via GitHub itself; "
    "removing them from the GitHub App revokes access regardless of role."
)
