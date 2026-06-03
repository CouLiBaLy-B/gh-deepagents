"""FastAPI server: GitHub webhooks + multi-tenant admin API.

Two auth paths:
- ``POST /webhook``: HMAC signature only (no user auth — GitHub calls us)
- All other endpoints: ``Authorization: Bearer <token>``
    * GitHub user access token (validated against GitHub API, scopes to user's
      installations)
    * Admin token (``DEEPAGENT_ADMIN_TOKEN``) bypasses scoping

Set ``DEEPAGENT_AUTH_DISABLED=1`` to turn off auth entirely (dev only).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import asdict
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ..config import get_settings
from ..observability import setup_observability
from ..observability.audit import audit_log, get_audit_store
from ..observability.cost_tenant import get_store as get_tenant_cost_store
from ..observability.logging_setup import get_logger
from ..observability.metrics import DLQ_SIZE, JOBS_TOTAL, QUEUE_DEPTH, QUOTA_REJECTIONS
from ..observability.tracing import current_traceparent, span
from .auth_tokens import UserContext, get_verifier
from .roles import Role, effective_role, get_role_store

setup_observability(service_name="gh-deepagent-webhook")
log = get_logger("gh_deepagent.webhook")
stdlib_log = logging.getLogger("gh_deepagent.webhook")
stdlib_log.setLevel(logging.INFO)


# ============================================================== DEPENDENCIES

def _bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def require_user(authorization: Optional[str] = Header(default=None)) -> UserContext:
    """FastAPI dependency: returns the authenticated user or raises 401."""
    tok = _bearer_token(authorization)
    ctx = get_verifier().verify(tok)
    if ctx is None:
        raise HTTPException(401, "Invalid or missing bearer token.")
    return ctx


def require_admin(user: UserContext = Depends(require_user)) -> UserContext:
    if not user.is_admin:
        raise HTTPException(403, "Admin privileges required.")
    return user


def require_role_on_installation(installation_id: int, user: UserContext, role: Role) -> Role:
    """Return the user's effective role, or raise 403/404 if insufficient.

    404 (not 403) for non-members so we don't leak installation existence.
    """
    eff = effective_role(user, installation_id)
    if eff is None:
        raise HTTPException(404, "installation not found")
    if not eff.can(role):
        raise HTTPException(403, f"requires role {role.value}, you are {eff.value}")
    return eff


# ============================================================== APP

def _make_app() -> FastAPI:
    app = FastAPI(title="gh-deepagent webhook", version="0.5.0")
    from ..queue import Job, JobQueue
    from ..queue.quota import QuotaManager

    queue = JobQueue()
    quotas = QuotaManager(client=queue._r)

    # ------------ helpers ----------
    def _verify_signature(body: bytes, header_sig: str | None) -> None:
        secret = os.getenv("DEEPAGENT_WEBHOOK_SECRET", "")
        if not secret:
            return
        if not header_sig or not header_sig.startswith("sha256="):
            raise HTTPException(401, "Missing or malformed X-Hub-Signature-256.")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, header_sig):
            raise HTTPException(401, "Invalid webhook signature.")

    def _scope_or_404(job: Optional[Job], user: UserContext,
                      min_role: Role = Role.VIEWER) -> Job:
        """Validate access to a job, checking role on its installation."""
        if not job:
            raise HTTPException(404, "job not found")
        if user.is_admin:
            return job
        if job.installation_id is None:
            # Legacy PAT-mode jobs — only admins see them.
            raise HTTPException(404, "job not found")
        eff = effective_role(user, job.installation_id)
        if eff is None:
            raise HTTPException(404, "job not found")
        if not eff.can(min_role):
            raise HTTPException(403, f"requires role {min_role.value}, you are {eff.value}")
        return job

    # ============================================================ public
    @app.get("/healthz")
    async def healthz():
        redis_ok = queue.ping()
        stats = queue.stats() if redis_ok else {"queue_depth": -1, "dead_letter": -1}
        return JSONResponse(
            {"status": "ok" if redis_ok else "degraded", "redis": redis_ok, **stats, "version": "0.5.0"},
            status_code=200 if redis_ok else 503,
        )

    # ============================================================ admin-only
    @app.get("/metrics")
    async def metrics(_admin: UserContext = Depends(require_admin)):
        try:
            s = queue.stats()
            QUEUE_DEPTH.set(s["queue_depth"])
            DLQ_SIZE.set(s["dead_letter"])
        except Exception:
            log.exception("metrics stats failed")
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/dlq")
    async def dlq(limit: int = 50, _admin: UserContext = Depends(require_admin)):
        return [
            {"id": j.id, "event": j.event, "repo": j.repo_full_name,
             "error": j.error, "attempts": j.attempts,
             "installation_id": j.installation_id}
            for j in queue.list_dead(limit=limit)
        ]

    @app.post("/dlq/{job_id}/requeue")
    async def requeue_dead(job_id: str, user: UserContext = Depends(require_user)):
        """Requeue a DLQ job. Allowed to global admins OR install operators."""
        job = queue.get(job_id)
        if not job:
            raise HTTPException(404, "job not in DLQ")
        # Permission check: global admin OR operator on the job's installation.
        if not user.is_admin:
            require_role_on_installation(job.installation_id, user, Role.OPERATOR)
        ok = queue.requeue_dead(job_id)
        if not ok:
            raise HTTPException(404, "job not in DLQ")
        audit_log(
            actor=user.login, via=user.via, action="dlq.requeue",
            target=job_id, installation_id=job.installation_id,
        )
        return {"requeued": True, "job_id": job_id}

    # ============================================================ webhook (GitHub HMAC)
    @app.post("/webhook")
    async def webhook(
        request: Request,
        x_hub_signature_256: str | None = Header(default=None),
        x_github_event: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
    ):
        body = await request.body()
        _verify_signature(body, x_hub_signature_256)
        if not x_github_event:
            raise HTTPException(400, "Missing X-GitHub-Event header.")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"Invalid JSON: {e}") from e

        if x_github_delivery and queue.already_seen(x_github_delivery):
            JOBS_TOTAL.labels(x_github_event, "deduped").inc()
            return {"deduped": True}

        settings = get_settings()
        if not _is_actionable(x_github_event, payload, settings):
            JOBS_TOTAL.labels(x_github_event, "ignored").inc()
            return {"ignored": True, "reason": "event does not match triggers"}

        repo_full = payload["repository"]["full_name"]
        installation_id = (payload.get("installation") or {}).get("id")

        decision = quotas.check_and_consume(installation_id)
        if not decision.allowed:
            QUOTA_REJECTIONS.labels(str(installation_id or "anon")).inc()
            JOBS_TOTAL.labels(x_github_event, "quota_rejected").inc()
            raise HTTPException(
                429,
                detail={
                    "error": "quota_exceeded", "bucket": decision.bucket,
                    "current": decision.current, "limit": decision.limit,
                    "retry_after_seconds": decision.retry_after_seconds,
                },
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )

        with span("job.enqueue", repo=repo_full, gh_event=x_github_event,
                  installation_id=installation_id) as _s:
            traceparent = current_traceparent()
            payload_with_trace = dict(payload)
            if traceparent:
                payload_with_trace.setdefault("_deepagent", {})["traceparent"] = traceparent
            job = Job.new(
                event=x_github_event, repo_full_name=repo_full,
                payload=payload_with_trace, installation_id=installation_id,
                delivery_id=x_github_delivery,
            )
            queue.enqueue(job)
            if _s is not None:
                try: _s.set_attribute("job.id", job.id)
                except Exception: pass

        JOBS_TOTAL.labels(x_github_event, "queued").inc()
        QUEUE_DEPTH.set(queue.stats()["queue_depth"])
        log.info("job enqueued", job_id=job.id, repo=repo_full, gh_event=x_github_event)
        audit_log(
            actor="github", via="webhook", action="job.create",
            target=job.id, installation_id=installation_id,
            event=x_github_event, repo=repo_full,
        )
        return {"accepted": True, "job_id": job.id, "event": x_github_event}

    # ============================================================ user-scoped
    @app.get("/me")
    async def whoami(user: UserContext = Depends(require_user)):
        return {
            "login": user.login,
            "is_admin": user.is_admin,
            "via": user.via,
            "installation_ids": sorted(user.installation_ids),
        }

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str, user: UserContext = Depends(require_user)):
        job = _scope_or_404(queue.get(job_id), user)
        d = asdict(job)
        d["status"] = job.status.value
        return d

    @app.get("/jobs/{job_id}/logs")
    async def get_job_logs(job_id: str, tail: int = 200,
                           user: UserContext = Depends(require_user)):
        _scope_or_404(queue.get(job_id), user)
        return {"job_id": job_id, "lines": queue.get_logs(job_id, tail=tail)}

    @app.get("/jobs/{job_id}/stream")
    async def stream_job_logs(job_id: str, replay: bool = True,
                              user: UserContext = Depends(require_user)):
        _scope_or_404(queue.get(job_id), user)

        def _sse(event: str, data: str) -> bytes:
            payload = data.replace("\n", "\ndata: ")
            return f"event: {event}\ndata: {payload}\n\n".encode()

        def _gen():
            if replay:
                for line in queue.get_logs(job_id, tail=queue.LOG_CAP):
                    yield _sse("log", line)
            current = queue.get(job_id)
            if current and current.status.value in {"succeeded", "failed", "dead"}:
                yield _sse("status", json.dumps({
                    "status": current.status.value,
                    "result": current.result, "error": current.error,
                }))
                return
            heartbeat = 0
            for chunk in queue.subscribe_logs(job_id):
                if chunk is None:
                    heartbeat += 1
                    if heartbeat % 15 == 0:
                        yield b": keepalive\n\n"
                    continue
                if chunk.startswith("{") and "_status" in chunk:
                    try:
                        data = json.loads(chunk)
                        yield _sse("status", json.dumps({
                            "status": data.get("_status"),
                            "result": data.get("_result"),
                            "error": data.get("_error"),
                        }))
                        if data.get("_status") in {"succeeded", "failed", "dead"}:
                            return
                    except Exception:
                        yield _sse("log", chunk)
                else:
                    yield _sse("log", chunk)

        return StreamingResponse(
            _gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"},
        )

    @app.get("/installations/{installation_id}/quota")
    async def installation_quota(installation_id: int,
                                 user: UserContext = Depends(require_user)):
        require_role_on_installation(installation_id, user, Role.VIEWER)
        return {"installation_id": installation_id, "usage": quotas.usage(installation_id)}

    @app.get("/installations/{installation_id}/jobs")
    async def installation_jobs(installation_id: int, limit: int = 100,
                                user: UserContext = Depends(require_user)):
        require_role_on_installation(installation_id, user, Role.VIEWER)
        jobs = queue.list_for_installation(installation_id, limit=limit)
        return [
            {"id": j.id, "event": j.event, "repo": j.repo_full_name,
             "status": j.status.value, "created_at": j.created_at,
             "finished_at": j.finished_at, "error": j.error,
             "attempts": j.attempts}
            for j in jobs
        ]

    # ---------- A. Per-tenant cost ----------
    @app.get("/installations/{installation_id}/cost")
    async def installation_cost(installation_id: int,
                                user: UserContext = Depends(require_user)):
        require_role_on_installation(installation_id, user, Role.VIEWER)
        return get_tenant_cost_store().usage(installation_id)

    @app.post("/installations/{installation_id}/cost/reset")
    async def reset_installation_cost(installation_id: int,
                                      user: UserContext = Depends(require_user)):
        require_role_on_installation(installation_id, user, Role.ADMIN)
        get_tenant_cost_store().reset(installation_id)
        audit_log(actor=user.login, via=user.via, action="cost.reset",
                  installation_id=installation_id)
        return {"reset": True, "installation_id": installation_id}

    @app.get("/installations")
    async def list_installations(user: UserContext = Depends(require_user)):
        """List installations visible to this user with the cost store knowledge."""
        if user.is_admin:
            iids = sorted(set(_all_installations(queue))
                          | set(get_tenant_cost_store().list_installations()))
        else:
            iids = sorted(user.installation_ids)
        return [
            {"installation_id": iid,
             "role": (effective_role(user, iid).value if effective_role(user, iid) else None)}
            for iid in iids
        ]

    # ---------- B. Roles per installation ----------
    @app.get("/installations/{installation_id}/roles")
    async def list_roles(installation_id: int,
                         user: UserContext = Depends(require_user)):
        require_role_on_installation(installation_id, user, Role.VIEWER)
        roles = get_role_store().list(installation_id)
        return {
            "installation_id": installation_id,
            "roles": {login: r.value for login, r in roles.items()},
        }

    @app.put("/installations/{installation_id}/roles/{login}")
    async def set_role(installation_id: int, login: str,
                       role: str,
                       user: UserContext = Depends(require_user)):
        require_role_on_installation(installation_id, user, Role.ADMIN)
        parsed = Role.parse(role)
        if parsed is None:
            raise HTTPException(400, f"invalid role {role!r}; must be viewer/operator/admin")
        assignment = get_role_store().set(installation_id, login, parsed, granted_by=user.login)
        audit_log(actor=user.login, via=user.via, action="role.grant",
                  target=login, installation_id=installation_id,
                  role=parsed.value)
        return {"installation_id": installation_id, "login": assignment.login,
                "role": assignment.role.value, "granted_by": assignment.granted_by}

    @app.delete("/installations/{installation_id}/roles/{login}")
    async def remove_role(installation_id: int, login: str,
                          user: UserContext = Depends(require_user)):
        require_role_on_installation(installation_id, user, Role.ADMIN)
        if get_role_store().remove(installation_id, login, removed_by=user.login):
            audit_log(actor=user.login, via=user.via, action="role.revoke",
                      target=login, installation_id=installation_id)
            return {"removed": True}
        raise HTTPException(404, "no such role assignment")

    # ---------- C. Audit log ----------
    @app.get("/audit")
    async def audit_global(limit: int = 200, _admin: UserContext = Depends(require_admin)):
        return get_audit_store().tail_global(limit=limit)

    @app.get("/installations/{installation_id}/audit")
    async def audit_for_installation(installation_id: int, limit: int = 200,
                                     user: UserContext = Depends(require_user)):
        require_role_on_installation(installation_id, user, Role.VIEWER)
        return get_audit_store().tail_for_installation(installation_id, limit=limit)

    @app.get("/jobs")
    async def list_jobs(limit_per_install: int = 50,
                        user: UserContext = Depends(require_user)):
        """List recent jobs across every installation the user can see."""
        out: list[dict] = []
        for iid in (sorted(user.installation_ids) if not user.is_admin else _all_installations(queue)):
            for j in queue.list_for_installation(iid, limit=limit_per_install):
                out.append({
                    "id": j.id, "event": j.event, "repo": j.repo_full_name,
                    "status": j.status.value, "created_at": j.created_at,
                    "installation_id": j.installation_id,
                })
        out.sort(key=lambda d: d["created_at"], reverse=True)
        return out

    return app


def _all_installations(queue) -> list[int]:
    """For admins: scan Redis for known installation indices."""
    out: list[int] = []
    try:
        for key in queue._r.scan_iter(match=f"{queue.KEY_PREFIX}:install_idx:*", count=100):
            k = key.decode() if isinstance(key, bytes) else key
            try:
                out.append(int(k.rsplit(":", 1)[-1]))
            except ValueError:
                continue
    except Exception:
        pass
    return sorted(set(out))


def _is_actionable(event: str, payload: dict, settings) -> bool:
    if event == "issues":
        action = payload.get("action")
        labels = [lbl["name"] for lbl in payload["issue"].get("labels", [])]
        if action == "labeled" and payload["label"]["name"] == settings.trigger_label:
            return True
        if action == "opened" and settings.trigger_label in labels:
            return True
        return False
    if event == "issue_comment":
        if payload.get("action") != "created":
            return False
        body = (payload["comment"]["body"] or "").strip()
        return body.startswith(settings.command_prefix)
    if event == "pull_request":
        action = payload.get("action")
        labels = [lbl["name"] for lbl in payload["pull_request"].get("labels", [])]
        return action in ("labeled", "opened") and settings.review_label in labels
    return False


app = _make_app()
