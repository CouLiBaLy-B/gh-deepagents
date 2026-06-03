# Multi-tenant SaaS mode

This guide walks through enabling the SaaS-style features added in v0.5:

1. **GitHub OAuth login** on the Streamlit dashboard (Device Flow)
2. **Per-installation scoping** on the webhook admin API

After this, users authenticate with their GitHub account and only see jobs from
the GitHub App installations they have access to. Admins keep the full view.

## TL;DR config

```env
# webhook
DEEPAGENT_ADMIN_TOKEN=ops-shared-secret           # comma-sep, optional
DEEPAGENT_ADMIN_GITHUB_LOGINS=alice,bob           # optional
DEEPAGENT_AUTH_DISABLED=0                         # 1 = open API (dev only)

# dashboard
DEEPAGENT_OAUTH_CLIENT_ID=Iv1.xxxxxxxxxxxxxxxx    # your GitHub OAuth App
```

## Step 1 — create a GitHub OAuth App

Settings → Developer settings → OAuth Apps → **New OAuth App**.

| Field                    | Value                                                |
|--------------------------|------------------------------------------------------|
| Application name         | `gh-deepagent dashboard`                             |
| Homepage URL             | `https://your-dashboard.example.com`                 |
| Authorization callback   | `http://localhost` (unused but required)             |
| ✅ Enable Device Flow    | yes                                                  |

Copy the **Client ID** (starts with `Iv1.`) → `DEEPAGENT_OAUTH_CLIENT_ID`.

This is a normal OAuth App, **not** the GitHub App that the bot acts as. Two
different things:

- **GitHub App** = the bot identity (clones repos, opens PRs)
- **OAuth App** = lets users sign in to the dashboard with their GitHub account

## Step 2 — admin escalation

Two ways to grant admin (sees all installations, can requeue DLQ, view cost):

```env
# Static token — anyone presenting it is admin. Useful for ops scripts.
DEEPAGENT_ADMIN_TOKEN=8f2e...   # comma-separated for multiple

# Auto-promote specific GitHub logins after OAuth login.
DEEPAGENT_ADMIN_GITHUB_LOGINS=alice,bob
```

Logins are matched case-insensitively. Empty = no auto-promotion.

## Step 3 — turn it on

```bash
docker compose -f deploy/docker-compose.yml up -d --build
# Webhook on :8080, dashboard on :8501
```

Visit `http://host:8501`. You'll see the login screen:

- **GitHub (Device Flow)** — click *Sign in with GitHub*, copy the one-time code,
  visit `github.com/login/device`, paste it, approve. Dashboard polls and
  redirects automatically.
- **Paste token** — for ops users, or to test with a personal access token
  during setup.

## How scoping works on the server

Every admin endpoint requires `Authorization: Bearer <token>`. The
:class:`TokenVerifier`:

```
token presented
    │
    ├─ DEEPAGENT_AUTH_DISABLED=1 ?      → anonymous admin (dev only)
    ├─ matches DEEPAGENT_ADMIN_TOKEN ?  → admin (no scoping)
    └─ otherwise:
            GET https://api.github.com/user            → login
            GET .../user/installations                 → installation_ids
            login ∈ DEEPAGENT_ADMIN_GITHUB_LOGINS ?    → admin
            ↳ result cached 5 min (SHA-256 of token, never raw)
```

Per-job scoping:

```python
def can_see_installation(self, iid):
    if self.is_admin: return True
    return iid in self.installation_ids
```

Endpoints behave like this:

| Endpoint                             | Visibility                                          |
|--------------------------------------|------------------------------------------------------|
| `GET /healthz`                       | public                                              |
| `POST /webhook`                      | HMAC only (no user token)                           |
| `GET /me`                            | authenticated user                                  |
| `GET /jobs/{id}`                     | owner of `installation_id`, or admin (else **404**) |
| `GET /jobs/{id}/logs|stream`         | same                                                |
| `GET /jobs`                          | scoped to user's installations (admin → all)        |
| `GET /installations/{id}/quota|jobs` | owner only, else **404**                            |
| `GET /metrics`                       | admin only                                          |
| `GET /dlq` / `POST /dlq/*/requeue`   | admin only                                          |

We return **404 (not 403)** on cross-tenant lookups so an attacker can't probe
existence of jobs they don't own.

## Sub-resource: per-installation indexing

`JobQueue.enqueue()` now also pushes the job ID into a per-installation
Redis list (`deepagent:install_idx:<id>`, capped at 1000 entries). That powers
`GET /installations/{id}/jobs` and the tenant view on the dashboard's home
page — without scanning every job in Redis.

## What a regular user sees

```
🏠 Overview         — KPIs computed from their own jobs only
📋 Jobs             — input any job ID they own; live SSE tail
🏢 Installations    — drop-down of their installations + quota usage
💀 DLQ              — *blocked, admin-only*
💸 Cost             — *blocked, admin-only*
🚀 Trigger          — *blocked, admin-only*
```

What an admin sees: everything, no scoping.

## What we deliberately do NOT do

- **Per-tenant cost attribution** — the LLM cost counter is global. Splitting
  it by installation requires either passing labels through every callback
  (intrusive) or aggregating job durations × model rates in Redis. Defer until
  there's a customer who needs to bill back.
- **Audit log** — every action goes through a single structured log line
  (`structlog`), which is enough for a Loki query. No separate audit DB.
- **Email/SSO** — only GitHub OAuth. Adding Google / Microsoft / SAML is
  ~50 LOC if needed: implement another `request_code` / `poll_once` pair and
  the rest of the verifier stays the same.

## Sanity-check after deployment

```bash
# /healthz works without a token
curl -fsS https://api.example.com/healthz | jq

# Admin token bypasses scoping
curl -fsS -H "Authorization: Bearer $DEEPAGENT_ADMIN_TOKEN" \
    https://api.example.com/me | jq

# A GitHub user PAT scopes correctly
curl -fsS -H "Authorization: Bearer $GH_PAT" \
    https://api.example.com/me | jq
# → {"login":"...","is_admin":false,"installation_ids":[...]}

# Random tokens get 401
curl -i -H "Authorization: Bearer nope" https://api.example.com/me
# → HTTP/1.1 401 Unauthorized
```

## Migrating from open-API (v0.4 → v0.5)

The CLI and existing scripts that used to hit `/jobs` etc. without auth will
now get 401. Options:

1. **Mint a long-lived admin token** and add it to scripts:
   ```bash
   curl -H "Authorization: Bearer $DEEPAGENT_ADMIN_TOKEN" .../dlq
   ```
2. **Temporary opt-out** during the migration:
   ```env
   DEEPAGENT_AUTH_DISABLED=1
   ```
   ⚠️ This is **dev-only** — never expose an open admin API to the Internet.
