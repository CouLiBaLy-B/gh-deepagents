---
title: gh-deepagent (demo)
emoji: 🤖
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: "All-in-one demo: webhook + workers + Redis + dashboard"
---

# gh-deepagent — All-in-one demo

⚠️ **This is a DEMO build for trying the project quickly.** Everything (Redis,
the webhook, one worker, the Streamlit dashboard) runs inside a single Space
container under `supervisord`. State is lost when the Space restarts.

For production, deploy the components separately on real infrastructure
(see `docs/QUEUE_AND_OBSERVABILITY.md`).

## What runs inside

| Process     | Port (internal) | Exposed to public? |
|-------------|----------------|--------------------|
| redis-server| 6379           | no                 |
| webhook     | 8080           | no (loopback only) |
| worker (×1) | —              | no                 |
| streamlit   | 7860           | **yes** ← Space root |

The dashboard reaches the webhook via `http://127.0.0.1:8080` (loopback).

⚠️ **GitHub cannot call this Space's webhook directly** — it isn't exposed.
This Space is for *trying the UI*, not for receiving real GitHub events.
For production, deploy webhook + workers on a real VPS (see `docs/QUEUE_AND_OBSERVABILITY.md`).

## Required Space secrets

In *Settings → Variables and secrets*:

| Key                          | Why                                              |
|------------------------------|--------------------------------------------------|
| `GITHUB_TOKEN`               | PAT (or use the GitHub App pair below)           |
| `DEEPAGENT_GITHUB_APP_ID`    | optional, prefer over PAT                        |
| `DEEPAGENT_GITHUB_APP_PRIVATE_KEY` | the .pem contents (multiline OK)           |
| `DEEPAGENT_WEBHOOK_SECRET`   | HMAC secret if GitHub will hit this Space        |
| `DEEPAGENT_OAUTH_CLIENT_ID`  | GitHub OAuth App Client ID (Device Flow)         |
| `DEEPAGENT_ADMIN_GITHUB_LOGINS` | your handle, comma-separated                 |
| `DEEPAGENT_MODEL`            | e.g. `anthropic:claude-sonnet-4-5`               |
| `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` | depending on the model               |

## Receiving GitHub webhooks

Spaces are reachable at `https://<user>-<space>.hf.space`. Configure your
GitHub App's webhook URL to `https://<user>-<space>.hf.space/webhook`. The
HMAC signature is verified using `DEEPAGENT_WEBHOOK_SECRET`.

Be aware of the **per-Space request timeout** — long-running webhook events
should still be fine because we respond 202 immediately and process in the
background.

## Limits

- Free CPU tier only. No GPU for Ollama → use a hosted model.
- Container sleeps if idle. First request wakes it up (~10–30 s).
- Redis is in-memory; jobs are lost on restart.

For a serious deployment, see the `deploy/docker-compose.yml` in the source
repo.
