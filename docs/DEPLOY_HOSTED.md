# Hosted deployments — Hugging Face Spaces & Streamlit Community Cloud

Three CI workflows ship the project to free / cheap hosted platforms whenever
you push to `main`. Pick the one that matches your audience.

## Decision tree

```
What do you want public?
│
├── Just the dashboard (agent runs on my own VPS)
│     │
│     ├── Need a Docker container?
│     │     YES → HF Spaces (dashboard)            → workflow: deploy-hf-dashboard.yml
│     │     NO  → Streamlit Community Cloud        → workflow: deploy-streamlit-cloud.yml
│     │
│     └── These two are equivalent for an end user. Pick the platform you
│         already have an account on.
│
└── The whole stack (webhook + workers + dashboard) for a quick demo
       → HF Spaces (all-in-one)                    → workflow: deploy-hf-allinone.yml
       ⚠️ Demo-only: ephemeral Redis, single CPU, no GPU.
```

## Target 1 — HF Spaces, dashboard only

| Path                                  | What                                       |
|---------------------------------------|--------------------------------------------|
| `deploy/huggingface/dashboard/README.md`  | YAML frontmatter (`sdk: docker`, port 7860) |
| `deploy/huggingface/dashboard/Dockerfile` | Python 3.12 + Streamlit, runs as UID 1000 |
| `.github/workflows/deploy-hf-dashboard.yml` | Mirrors to HF on every push to main     |

**One-time setup:**

1. Create a [new Space](https://huggingface.co/new-space): pick *Docker*,
   note the full name (`<user>/<space>`).
2. Generate a [HF write token](https://huggingface.co/settings/tokens).
3. In the source GitHub repo → *Settings → Variables and secrets → Actions*:
   - **Variable** `HF_USER`             = your HF username
   - **Variable** `HF_DASHBOARD_SPACE`  = `<user>/<space>` (e.g. `alice/gh-deepagent`)
   - **Secret**   `HF_TOKEN`            = the write token
4. In the Space → *Settings → Variables and secrets*:
   - **Variable** `DEEPAGENT_API_URL`        = `https://your-webhook.example.com`
   - **Variable** `DEEPAGENT_OAUTH_CLIENT_ID`= `Iv1.xxxxxxxx`

Push to `main` → the workflow mirrors the dashboard subset of the source tree
into the Space repo. HF rebuilds the image, restarts the container, ~2 min.

## Target 2 — HF Spaces, all-in-one demo

Same as above but pointed at the all-in-one Dockerfile, which boots
`redis-server + webhook + worker + streamlit + router` under `supervisord`.

**Extra setup:**

- Variable `HF_ALLINONE_SPACE` instead of `HF_DASHBOARD_SPACE`.
- A small reverse proxy (`deploy/huggingface/all-in-one/router.py`) multiplexes
  the dashboard and the `/webhook` endpoint behind the same port 7860.
- In the Space settings, add the secrets needed by the agent
  (`GITHUB_TOKEN` or App credentials, `DEEPAGENT_WEBHOOK_SECRET`,
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` depending on the model).

If you want GitHub to actually call this Space, point your App's webhook URL
to `https://<user>-<space>.hf.space/webhook`. HMAC signature validation
applies as usual.

## Target 3 — Streamlit Community Cloud

Streamlit Cloud auto-deploys on every push to a branch you select.

**One-time setup:**

1. Sign in at <https://share.streamlit.io>.
2. Push at least once on `main` so the workflow creates the
   **`streamlit-cloud`** branch (auto-generated, do not edit).
3. Click *New app*, pick this repo, branch `streamlit-cloud`, file
   `streamlit_app.py`, Python 3.11+.
4. Click *Advanced → Secrets* and paste:
   ```toml
   DEEPAGENT_API_URL          = "https://your-webhook.example.com"
   DEEPAGENT_OAUTH_CLIENT_ID  = "Iv1.xxxxxxxxxxxxxxxx"
   ```
5. Deploy. Subsequent pushes to `main` trigger our workflow that updates the
   `streamlit-cloud` branch, which in turn triggers Streamlit Cloud's own
   rebuild. Two-stage but fully automatic.

**Why a dedicated branch?**

Streamlit Cloud expects the entrypoint, `requirements.txt`, and `.streamlit/`
at the repo root. Putting them there pollutes `main` for everyone working on
the project (and conflicts with our Python source layout). The workflow keeps
them isolated on `streamlit-cloud` — your `main` stays clean.

## Cost comparison

| Target                      | Tier             | Limits                                  | Use case                  |
|-----------------------------|------------------|-----------------------------------------|---------------------------|
| HF Spaces — dashboard       | CPU Basic (free) | 16 GB RAM, sleeps idle                  | Public read-only dashboard|
| HF Spaces — all-in-one      | CPU Basic (free) | Same; ephemeral Redis                   | Quick demo                |
| Streamlit Community Cloud   | Free             | ~1 vCPU / 1 GB, sleeps after 7 d        | Personal/small-team UI    |

For real production traffic (high QPS, persistent Redis, GPU-backed Ollama)
none of these are suitable — use the Docker Compose setup in `deploy/`.

## What happens if a deploy fails

Each workflow logs the diff that was about to be pushed (see the `Stage Space
contents` step) and skips committing if the tree is unchanged. The HF token
errors are made very explicit (`HF_TOKEN secret required`) so a missing
secret never produces a silent failure.

On the HF Space side, the build log is visible in the Space's *Logs* tab —
that's where you'll see Python install failures or runtime crashes.

## Local dry-run before pushing

```bash
# Test the dashboard image locally exactly as HF will build it
docker build -t dashboard:test -f deploy/huggingface/dashboard/Dockerfile .
docker run --rm -p 7860:7860 -e DEEPAGENT_API_URL=http://host.docker.internal:8080 \
    dashboard:test

# Test the all-in-one
docker build -t allinone:test -f deploy/huggingface/all-in-one/Dockerfile .
docker run --rm -p 7860:7860 -e GITHUB_TOKEN=ghp_xxx allinone:test

# Run the Streamlit Cloud layout locally
cd deploy/streamlit-cloud
pip install -r requirements.txt
DEEPAGENT_API_URL=http://localhost:8080 streamlit run streamlit_app.py
```

The `ci.yml` workflow runs the Docker builds on every PR so a broken
Dockerfile is caught before it can reach a Space.
