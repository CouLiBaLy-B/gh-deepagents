# SaaS features (v0.6): cost-per-tenant, roles, audit log

Three features that turn the multi-tenant base into something you could
actually bill, govern, and audit.

## A. Per-installation cost attribution

Every LLM call is now attributed to the installation that triggered the job —
so you can answer "how much did installation X spend this month?" without
fancy log queries.

### How it works

```
worker._process(job)
  ├── bind_installation(job.installation_id)   # contextvar
  │
  ├── agent.invoke(...)
  │     └── CostCallback.on_llm_end()
  │          ├── global counters (Prometheus)            # unchanged
  │          └── TenantCostStore.record(iid, ...)         # NEW
  │
  └── unbind_installation()                              # finally:
```

The :class:`ContextVar` is per-OS-thread so two concurrent workers in the same
process never cross-contaminate. CLI runs leave the contextvar unset → the
callback skips per-tenant accounting (only global metrics fire).

### Endpoints

```
GET  /installations/{id}/cost          → {total_usd, models: {provider:model: {usd, input_tokens, output_tokens}}}
POST /installations/{id}/cost/reset    → admin-only; starts a new billing period
```

Visible to: anyone with `viewer` role on the installation.

### Dashboard

The **💸 Cost** page now drops the global view and shows a per-installation
breakdown with a selector. Admins can reset the counter from a confirmation
modal.

## B. Roles per installation

Three roles, strictly ordered:

| Role       | Read jobs/quotas | Requeue DLQ | Manage roles | Reset cost |
|------------|:----------------:|:-----------:|:------------:|:----------:|
| `viewer`   | ✅                | ❌           | ❌            | ❌          |
| `operator` | ✅                | ✅           | ❌            | ❌          |
| `admin`    | ✅                | ✅           | ✅            | ✅          |

### Effective role resolution

```python
def effective_role(user, installation_id) -> Role | None:
    if user.is_admin:               return Role.ADMIN
    if installation_id not in user.installation_ids:  return None
    explicit = role_store.get(installation_id, user.login)
    return explicit or Role.VIEWER     # GitHub access defaults to viewer
```

Three layers, max wins:

1. **Global admin** (`DEEPAGENT_ADMIN_TOKEN` / `DEEPAGENT_ADMIN_GITHUB_LOGINS`)
   → admin everywhere.
2. **Explicit role** stored in Redis (`deepagent:role:<iid>`).
3. **Implicit viewer** if the user has GitHub App access.

The Redis role is **useless** without GitHub App access — that's intentional.
Removing a user from the GitHub App revokes everything regardless of role.

### Endpoints

```
GET    /installations/{id}/roles                 → {roles: {login: role}}
PUT    /installations/{id}/roles/{login}?role=X  → grant (admin-only)
DELETE /installations/{id}/roles/{login}         → revoke (admin-only)
```

### Dashboard

The new **👥 Roles** page lets installation admins grant / change / revoke
roles. Non-admins see a polite "ask your admin" message.

## C. Audit log

Every state-changing operation produces an :class:`AuditEvent` that lands in
**both** Redis and structlog:

- **Redis** for fast UI queries (`/audit`, `/installations/{id}/audit`),
  capped at 10 000 global / 1 000 per-install.
- **structlog** for long-term storage in your log aggregator (Loki, Datadog).

### Tracked actions

| Action            | Trigger                                | Includes                            |
|-------------------|----------------------------------------|-------------------------------------|
| `job.create`      | `POST /webhook` enqueues a job         | actor=github, repo, event           |
| `dlq.requeue`     | Operator or admin requeues a dead job  | actor, job id                       |
| `role.grant`      | Admin grants/changes a role            | actor, target login, new role       |
| `role.revoke`     | Admin removes a role assignment        | actor, target login                 |
| `cost.reset`      | Admin zeroes an installation's cost    | actor, installation id              |

Each event has: timestamp, actor (login or admin token prefix), via (github /
admin_token / webhook / system), action, target, optional installation_id,
free-form metadata (truncated to 1 KB).

### Endpoints

```
GET /audit                            → admin-only, last N global events
GET /installations/{id}/audit         → viewer+, last N events for that install
```

### Dashboard

The new **📜 Activity** page:

- Scope toggle (Global, admin-only / My installations)
- Filter by action substring and by actor
- Tabular view + top-actions and top-actors bar charts

## End-to-end example

1. **Webhook fires** → `job.create` audit event.
2. **Worker processes** → `bind_installation()` is set, cost callback writes to
   `deepagent:cost:by_install:42` and to `LLM_COST_USD` Prometheus counter.
3. **Job fails 3 times** → lands in DLQ.
4. **Operator on installation 42** requeues from the dashboard → `dlq.requeue`
   audit event, attributed to their login.
5. **Admin on installation 42** views the cost breakdown, decides to reset →
   `cost.reset` audit event.

Everything is traceable, scoped, and (mostly) self-service.

## Migration from v0.5

- `POST /dlq/{id}/requeue` was admin-only; it now ALSO accepts operators on
  the right installation. Existing admin-token scripts keep working.
- `GET /metrics` is still admin-only.
- New env vars: none. Roles and cost just need Redis (already required).
- Existing audit-style logs (structlog) are unchanged; the Redis-backed audit
  is additive.

## Tests

```
tests/test_cost_tenant.py    8 tests   (contextvar isolation, attribution, reset, callback wiring)
tests/test_roles.py         11 tests   (parsing, rank, CRUD, effective role layering)
tests/test_audit.py          5 tests   (persist + tail + per-install index + cap + truncation)
tests/test_webhook_v6.py    13 tests   (HTTP-level integration of cost, roles, audit, DLQ)
```

All run with `fakeredis` — no infrastructure needed.
