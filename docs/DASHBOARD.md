# Streamlit admin dashboard

A web UI that lets operators inspect the queue, debug jobs live, manage DLQ
items, audit quotas and review LLM cost — without ever ssh-ing into a box.

```
gh-deepagent dashboard
# → http://localhost:8501
```

It talks to the webhook server via its public HTTP endpoints
(`DEEPAGENT_API_URL`, default `http://localhost:8080`). No shared in-process
state, no privileged access — anything the dashboard does, you could `curl`.

## Pages

| Page              | What it shows                                                                  |
|-------------------|--------------------------------------------------------------------------------|
| 🏠 Overview       | KPIs (queue, DLQ, in-progress, spend), bar charts of jobs/sub-agents/tools     |
| 📋 Jobs           | Inspect a specific job by ID, snapshot logs **or live SSE tail**               |
| 💀 DLQ            | List dead-letter jobs, per-row + bulk requeue                                  |
| 🏢 Installations  | Quota usage (hour / day / concurrent) with progress bars                       |
| 💸 Cost           | Per-model breakdown (calls, in/out tokens, USD)                                |
| 🚀 Trigger        | One-off fix / evolve / review runs (bypasses the queue, runs in-process)       |

## Architecture

```
┌──────────────────┐    HTTP    ┌──────────────────┐
│ Streamlit (8501) │───────────▶│ Webhook (8080)   │
│  - Overview      │            │  /healthz        │
│  - Jobs          │            │  /metrics        │
│  - DLQ           │            │  /jobs/{id}      │
│  - Installations │            │  /jobs/{id}/...  │
│  - Cost          │            │  /dlq            │
│  - Trigger ────▶─┼──in-proc──▶│  runner.*        │
└──────────────────┘            └──────────────────┘
        │                              │
        │ SSE (text/event-stream)      │
        └──────────────────────────────┘
```

The **live tail** uses `httpx.stream("GET", "/jobs/<id>/stream")`. As the
generator yields SSE events, Streamlit overwrites a `st.empty()` placeholder
so the log buffer scrolls in real-time. The connection closes automatically
when the server sends a terminal status event.

## Configuration

| Env var                | Purpose                                                |
|------------------------|--------------------------------------------------------|
| `DEEPAGENT_API_URL`    | Webhook URL the dashboard talks to. Default `http://localhost:8080`. |

You can also change it on-the-fly from the sidebar — useful when running
the dashboard locally against a remote prod webhook.

## Authentication

The dashboard itself doesn't do auth. **Don't expose it to the public Internet.**
Put it behind:

- Tailscale / WireGuard (most common for ops dashboards)
- An OAuth2 reverse proxy (`oauth2-proxy`, `caddy-security`, Cloudflare Access)
- Basic auth via Caddy:
  ```caddy
  dashboard.example.com {
      basicauth {
          admin {env.ADMIN_HASH}
      }
      reverse_proxy gh-deepagent-dashboard:8501
  }
  ```

The Trigger page is especially powerful — anyone with access can open PRs
under the bot's identity. Keep it behind auth.

## Deployment (Docker Compose)

Already wired in `deploy/docker-compose.yml`:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
# Dashboard available at http://host:8501
```

## Standalone install

```bash
pip install -e ".[dashboard]"
gh-deepagent dashboard --api-url https://deepagent.example.com
```

## Customisation

All UI lives in `src/gh_deepagent/dashboard/`. To add a page, drop a
`pages/N_<emoji>_Name.py` file (Streamlit's multi-page convention). It will
appear in the sidebar automatically. Reuse `WebhookAPI` from `api.py` for any
new endpoint you want to surface.

## Limitations

- **Auto-refresh** is a busy-poll `st.rerun()` on a 10s timer — fine for an
  ops dashboard, don't open 100 tabs.
- **Trigger page** runs the agent in the Streamlit process and blocks the UI
  for the duration. For long jobs, prefer the queue path (label / comment).
- **No history view** — the dashboard reads the *current* Prometheus state.
  For time-series visualisation, use Grafana (it's already provisioned).
