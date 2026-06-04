"""LLM provider / model / API-key configuration page.

For the **all-in-one demo Space**, this page lets any admin reconfigure the LLM
**without restarting the container**. The settings are written to
``st.session_state`` and pushed into ``os.environ`` so that the in-process
``runner.fix_issue / evolve_code / review_pr`` calls (page 🚀 Trigger) pick
them up immediately.

Limitations:
- Workers running in *other processes* keep the env vars they were started
  with. For a multi-process deployment you still need to set DEEPAGENT_MODEL
  + the right API key at container-start time.
- For the standalone (no-backend) dashboard this page is informative — you
  can configure values for a future backend you'll deploy yourself.
"""
from __future__ import annotations

import os

import streamlit as st

from gh_deepagent.dashboard.auth_ui import render_user_badge, require_login


st.set_page_config(page_title="LLM Settings · gh-deepagent",
                   page_icon="⚙️", layout="wide")
st.title("⚙️ LLM provider, model & token")
st.caption(
    "Configure which LLM the agent should use. Changes apply **in this Space, "
    "right now**, for jobs triggered via the dashboard. They do NOT propagate "
    "to other Spaces or to workers in other processes."
)

_api, user = require_login()
render_user_badge()
if not user.get("is_admin"):
    st.error("LLM settings are restricted to admins.")
    st.stop()


# ----------------------------------------------------- known providers
PROVIDERS = {
    "anthropic": {
        "label": "Anthropic Claude",
        "env_key": "ANTHROPIC_API_KEY",
        "models": [
            "claude-sonnet-4-5",
            "claude-opus-4",
            "claude-haiku-4",
            "claude-sonnet-4-20250514",
        ],
        "spec_prefix": "anthropic:",
        "doc": "https://docs.anthropic.com/en/api/getting-started",
    },
    "openai": {
        "label": "OpenAI",
        "env_key": "OPENAI_API_KEY",
        "models": [
            "gpt-4o-mini",
            "gpt-4o",
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-5",
            "o4-mini",
        ],
        "spec_prefix": "openai:",
        "doc": "https://platform.openai.com/api-keys",
    },
    "google_genai": {
        "label": "Google Gemini",
        "env_key": "GOOGLE_API_KEY",
        "models": [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
        ],
        "spec_prefix": "google_genai:",
        "doc": "https://aistudio.google.com/apikey",
    },
    "groq": {
        "label": "Groq (Llama, Mixtral, …)",
        "env_key": "GROQ_API_KEY",
        "models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-70b-versatile",
            "mixtral-8x7b-32768",
        ],
        "spec_prefix": "groq:",
        "doc": "https://console.groq.com/keys",
    },
    "openrouter": {
        "label": "OpenRouter (unified — Anthropic, OpenAI, Llama, Mistral, …)",
        "env_key": "OPENROUTER_API_KEY",
        "models": [
            "anthropic/claude-sonnet-4-5",
            "anthropic/claude-haiku-4",
            "openai/gpt-4o-mini",
            "openai/gpt-4o",
            "openai/gpt-4.1-mini",
            "google/gemini-2.5-flash",
            "google/gemini-2.5-pro",
            "meta-llama/llama-3.3-70b-instruct",
            "mistralai/mistral-large-latest",
            "qwen/qwen-2.5-coder-32b-instruct",
            "deepseek/deepseek-chat",
        ],
        "spec_prefix": "openrouter:",
        "doc": "https://openrouter.ai/keys",
    },
    "ollama": {
        "label": "Ollama (local, no API key)",
        "env_key": "",  # no key
        "models": [
            "qwen2.5-coder:14b",
            "deepseek-coder-v2:16b",
            "llama3.1:8b",
        ],
        "spec_prefix": "ollama:",
        "doc": "https://ollama.com/library",
    },
}


# ----------------------------------------------------- current state
def _current_spec() -> str:
    return st.session_state.get("llm.spec") or os.getenv(
        "DEEPAGENT_MODEL", "anthropic:claude-sonnet-4-5"
    )


def _current_provider() -> str:
    spec = _current_spec()
    for pid, p in PROVIDERS.items():
        if spec.startswith(p["spec_prefix"]):
            return pid
    return "anthropic"


# ----------------------------------------------------- UI
st.subheader("Current configuration")

cur_provider = _current_provider()
cur_spec = _current_spec()
cur_model = cur_spec.split(":", 1)[1] if ":" in cur_spec else cur_spec

cc = st.columns(3)
cc[0].metric("Provider", PROVIDERS[cur_provider]["label"])
cc[1].metric("Model", cur_model)
key_env = PROVIDERS[cur_provider]["env_key"]
if key_env:
    key_set = bool(os.getenv(key_env))
    cc[2].metric(
        f"{key_env}",
        "✅ set" if key_set else "❌ missing",
        delta=None,
    )
else:
    cc[2].metric("API key", "—  (none required)")

st.divider()

# ----------------------------------------------------- form
st.subheader("Change configuration")

with st.form("llm-settings-form"):
    provider_id = st.selectbox(
        "Provider",
        list(PROVIDERS.keys()),
        index=list(PROVIDERS.keys()).index(cur_provider),
        format_func=lambda k: PROVIDERS[k]["label"],
    )
    p = PROVIDERS[provider_id]

    # Model selector: known models + free-text override
    model_options = p["models"] + ["(custom — type below)"]
    default_idx = (
        model_options.index(cur_model) if cur_model in model_options else 0
    )
    model_choice = st.selectbox("Model", model_options, index=default_idx)
    custom_model = st.text_input(
        "Custom model name",
        value=cur_model if cur_model not in p["models"] else "",
        disabled=model_choice != "(custom — type below)",
        placeholder="e.g. my-org/my-finetune",
    )
    chosen_model = custom_model if model_choice == "(custom — type below)" else model_choice

    api_key = ""
    if p["env_key"]:
        st.markdown(
            f"**API key** → stored as env var `{p['env_key']}` "
            f"([get one]({p['doc']}))"
        )
        api_key = st.text_input(
            f"{p['env_key']}",
            type="password",
            value="",  # never display the existing value
            placeholder="Leave blank to keep the current one",
        )
    else:
        st.caption(f"No API key needed for {p['label']}. "
                   f"Make sure an Ollama server is reachable.")

    base_url = ""
    if provider_id == "ollama":
        base_url = st.text_input(
            "OLLAMA_BASE_URL",
            value=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        )

    submitted = st.form_submit_button("Apply", type="primary")

if submitted:
    if not chosen_model:
        st.error("Pick or type a model name.")
        st.stop()

    spec = f"{p['spec_prefix']}{chosen_model}"
    os.environ["DEEPAGENT_MODEL"] = spec
    st.session_state["llm.spec"] = spec

    if api_key and p["env_key"]:
        os.environ[p["env_key"]] = api_key
        st.session_state[f"llm.key.{p['env_key']}"] = "set"  # marker only
    if base_url and provider_id == "ollama":
        os.environ["OLLAMA_BASE_URL"] = base_url

    # The Settings cache is frozen — invalidate it so the next get_settings()
    # picks up the new DEEPAGENT_MODEL.
    try:
        from gh_deepagent.config import get_settings
        get_settings.cache_clear()
    except Exception:
        pass

    st.success(f"✅ Applied: `{spec}`"
               + (f" with **{p['env_key']}**" if api_key else ""))
    st.info(
        "These settings apply to **jobs you launch from the dashboard right now**. "
        "They won't reach other workers / processes. For a permanent change, set "
        "the corresponding env vars in the Space *Settings → Variables and secrets*."
    )

st.divider()

# ----------------------------------------------------- diagnostics
with st.expander("🔎 What's in the environment right now?"):
    rows = []
    for pid, info in PROVIDERS.items():
        env = info["env_key"]
        if not env:
            continue
        rows.append({
            "Provider": info["label"],
            "Env var": env,
            "Set": "✅" if os.getenv(env) else "—",
        })
    rows.append({
        "Provider": "—",
        "Env var": "DEEPAGENT_MODEL",
        "Set": os.getenv("DEEPAGENT_MODEL", "(default)"),
    })
    rows.append({
        "Provider": "—",
        "Env var": "OLLAMA_BASE_URL",
        "Set": os.getenv("OLLAMA_BASE_URL", "(default)"),
    })
    import pandas as pd
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.caption(
    "💡 For production: configure these as Space *Variables* (model) and "
    "*Secrets* (API key). They'll then apply to every container restart, "
    "not just your current session."
)
