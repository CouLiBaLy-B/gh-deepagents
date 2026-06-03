"""Streamlit Community Cloud entrypoint.

This is a thin shim that imports the real dashboard app. It exists at the repo
root level (via the workflow) so Community Cloud finds it without us having to
flatten the project layout.

It also reads ``DEEPAGENT_API_URL`` from ``st.secrets`` (Community Cloud's
preferred place for env-style config) and surfaces it as a regular env var
before importing the app.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

# Make ``src/`` importable when running from the repo root.
_root = Path(__file__).parent.resolve()
for candidate in (_root / "src", _root.parent / "src", _root.parent.parent / "src"):
    if (candidate / "gh_deepagent").is_dir():
        sys.path.insert(0, str(candidate))
        break

# Map Community Cloud secrets → environment variables expected by the dashboard.
# (st.secrets reads from .streamlit/secrets.toml locally and from the Cloud UI
# in production.)
try:
    for key in (
        "DEEPAGENT_API_URL",
        "DEEPAGENT_OAUTH_CLIENT_ID",
        "DEEPAGENT_ADMIN_TOKEN",
        "DEEPAGENT_ADMIN_GITHUB_LOGINS",
    ):
        if key in st.secrets and not os.getenv(key):
            os.environ[key] = str(st.secrets[key])
except Exception:
    # secrets.toml might not exist locally; that's fine.
    pass

# Now execute the real app.
exec(compile((Path(sys.path[0]) / "gh_deepagent" / "dashboard" / "app.py").read_text(),
             "app.py", "exec"))
