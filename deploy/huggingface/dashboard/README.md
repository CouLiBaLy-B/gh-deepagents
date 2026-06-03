---
title: gh-deepagent dashboard
emoji: 🤖
colorFrom: indigo
colorTo: pink
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Admin dashboard for a self-hosted gh-deepagent webhook
---

# gh-deepagent — Dashboard (Hugging Face Space)

This Space hosts only the **Streamlit admin dashboard**. It is a *thin client*
that talks to your self-hosted `gh-deepagent` webhook server over HTTPS.

The agent itself (webhook + workers + Redis) runs on your own infrastructure
(VPS, k8s) — the Space is just a UI you can share with your team without
giving them shell access.

## Quick start (after the Space is built)

1. In the Space **Settings → Variables and secrets**:
   - `DEEPAGENT_API_URL`   = `https://your-webhook.example.com`
   - `DEEPAGENT_OAUTH_CLIENT_ID` = `Iv1.xxxxxxxx` (your GitHub OAuth App)
2. Open the Space URL → "Sign in with GitHub" (Device Flow) → done.

## Why is the agent not on the Space?

Spaces are great for stateless UIs but the agent needs:
- a Redis instance with persistence
- background workers running 24/7
- a fixed HTTPS endpoint GitHub can call

Those are better on a VPS. The Space is the **face**, not the engine.

For an all-in-one local demo (agent + dashboard inside the Space), see the
sibling `all-in-one` Space template.

## Source

This Space is auto-deployed from
[github.com/YOUR_ORG/gh-deepagent](https://github.com/YOUR_ORG/gh-deepagent)
on every push to `main`. See `.github/workflows/deploy-hf-dashboard.yml`.
