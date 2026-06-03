# gh-deepagent dashboard on Streamlit Community Cloud

A zero-Docker deployment of the **dashboard only**. The agent still runs on
your own VPS — Community Cloud is just a public, free Streamlit host.

## One-time setup

1. Push this repo to GitHub (or use a fork).
2. Visit <https://share.streamlit.io> → **New app** → connect the repo.
3. Branch: `main`, file: `streamlit_app.py` (the shim copied to the repo root
   by the CI workflow), Python version: 3.11+.
4. Click **Advanced settings → Secrets** and paste:
   ```toml
   DEEPAGENT_API_URL = "https://your-webhook.example.com"
   DEEPAGENT_OAUTH_CLIENT_ID = "Iv1.xxxxxxxxxxxxxxxx"
   ```
5. **Deploy**. Subsequent pushes to `main` redeploy automatically (handled by
   Streamlit Cloud itself — no GH Action needed for this target).

## What does the CI do, then?

The GitHub Actions workflow ``deploy-streamlit-cloud.yml`` only **prepares the
repo root** so Community Cloud's auto-deploy works:

- Copies `deploy/streamlit-cloud/streamlit_app.py` → `streamlit_app.py`
- Copies `deploy/streamlit-cloud/requirements.txt` → `requirements.txt`
- Copies `deploy/streamlit-cloud/.streamlit/config.toml` → `.streamlit/config.toml`

It commits these to a **`streamlit-cloud` branch** that you point Streamlit
Cloud at. Your `main` branch stays clean.

## Limits

- ~1 vCPU / 1 GB RAM free tier.
- App sleeps after ~7 days of inactivity (wake-up = 30 s).
- Public by default. To restrict access, use the *Viewer* settings in your
  Community Cloud app page (you can require a Google account).
- No Docker = no Redis client, no native LLM deps. The dashboard is fine
  because it's just an HTTP client over your webhook.
