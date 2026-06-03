# CI/CD reference

10 GitHub Actions workflows. Each has a single, well-scoped responsibility.

```
                                  ┌──────── push to main / PR ────────┐
                                  │                                    │
                                  ▼                                    │
                     ┌────────────────────────┐                        │
                     │ ci.yml                 │ ← always runs          │
                     │ pytest, ruff, docker   │                        │
                     │ build (no push)        │                        │
                     └───┬────────────────────┘                        │
                         │                                              │
        ┌────────────────┼──────────────────────────────────────┐       │
        │                │                                       │       │
        ▼                ▼                                       ▼       │
┌──────────────┐  ┌────────────────────┐               ┌────────────────┐│
│ release-     │  │ deploy-hf-         │               │ conventional-  ││
│ please       │  │ {dashboard,        │               │ pr             ││
│              │  │  allinone}         │               │ (PR only)      ││
│ rolling      │  │ + deploy-streamlit │               │ enforces       ││
│ release PR   │  │ -cloud             │               │ Conventional   ││
└──────┬───────┘  │ → HF & branch      │               │ Commits        ││
       │ tag      └──────┬─────────────┘               └────────────────┘│
       ▼                 │                                               │
┌──────────────┐         │           ┌──────────────────────────────────┘
│ publish-     │         │           │
│ image        │         │           ▼
│ ghcr.io      │         │     ┌──────────────────┐
│ multi-arch,  │         │     │ pr-preview-hf    │
│ cosign       │         │     │ ephemeral Space  │
└──────┬───────┘         │     │ per PR           │
       │                 │     └──────────────────┘
       │                 │
       ▼                 ▼
┌─────────────────────────────────────┐
│ rollback-on-failure                 │
│ (workflow_run: completed → success) │
│ healthcheck + auto revert if bad    │
└─────────────────────────────────────┘
```

## 1. `ci.yml` — testing & build verification

Runs on **every push and PR**. Three jobs in parallel:

- `test`: pytest on Python 3.11 + 3.12
- `lint`: ruff (advisory)
- `docker-build`: builds both HF Dockerfiles to catch breakage before deploy.
  Uses GHA cache so subsequent runs are ~30 s.

No artifacts are pushed anywhere. This is the safety net.

## 2. `release-please.yml` + `conventional-pr.yml` — automated versioning

`release-please` watches conventional commits on `main` and maintains a
**rolling "release PR"** (e.g. *"chore(main): release 0.7.0"*) that:

- bumps `__version__` in `src/gh_deepagent/__init__.py`
- regenerates `CHANGELOG.md` (grouped by `feat`/`fix`/`perf`/…)
- updates `.release-please-manifest.json`

Merging that PR creates a git tag (`v0.7.0`) and a GitHub Release.
The tag triggers `publish-image.yml`.

`conventional-pr.yml` blocks PRs whose title isn't `<type>(<scope>): <subject>`
so the squash-merge subject feeds release-please cleanly.

**Setup:** zero. The two JSON config files are committed and the workflow has
the right permissions.

## 3. `publish-image.yml` — GHCR multi-arch images + cosign

Fires on push to `main` and on `v*.*.*` tags. For each of two images
(`gh-deepagent`, `gh-deepagent-dashboard`):

1. Build per platform (`linux/amd64` + `linux/arm64`) in a 2×2 matrix
2. Push to GHCR by digest
3. Stitch a multi-arch manifest list with `docker buildx imagetools`
4. Sign keyless with cosign (GitHub OIDC) — no key management
5. Attach build provenance attestation (SLSA-style)

Tags applied:

| Trigger                 | Tags                                            |
|-------------------------|-------------------------------------------------|
| push to `main`          | `latest`, `main`, `sha-<short>`                 |
| release tag `v1.2.3`    | `1.2.3`, `1.2`, `1`, `latest`                   |

Pull:

```bash
docker pull ghcr.io/YOUR_ORG/gh-deepagent:latest
docker pull ghcr.io/YOUR_ORG/gh-deepagent-dashboard:1.2.3

# Verify signature
cosign verify ghcr.io/YOUR_ORG/gh-deepagent:1.2.3 \
  --certificate-identity-regexp 'https://github.com/YOUR_ORG/gh-deepagent/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

**Setup:** none — `permissions: packages: write` is granted in the workflow.
The first push creates the GHCR repo automatically. To make images public,
go to <https://github.com/users/YOUR_ORG/packages/container/gh-deepagent/settings>
→ *Change visibility*.

## 4. `pr-preview-hf.yml` — ephemeral preview Space per PR

For every PR from a collaborator (or labelled `preview-ok`):

1. Creates `<HF_USER>/gh-deepagent-pr-<NUM>` Space if missing (Docker SDK)
2. Pushes the PR's dashboard build to it
3. Comments on the PR with the live URL
4. Deletes the Space when the PR is closed

**Security**: triggered by `pull_request_target`, but a strict `authorize` job
checks that the PR author has write access OR the PR has the `preview-ok`
label (which only maintainers can apply). This prevents an external PR from
stealing `HF_TOKEN`.

**Setup:** add `vars.HF_USER` and `secrets.HF_TOKEN` (already required by the
main HF deploy workflows). The label `preview-ok` is optional — create it
manually in *Issues → Labels* if you want to override the gate.

## 5. `rollback-on-failure.yml` — auto-revert on failed deploy

Hooked to `workflow_run: completed` of the three deploy workflows (and the
GHCR publish). When one succeeds:

1. Poll `vars.PROD_URL/healthz` for 5 minutes (90 s grace).
2. If admin token is available, poll `/metrics` and compute failed/total
   error rate.
3. If degraded **OR** error rate > 25 %, open a `revert: rollback <SHA>` PR.
4. Open a tracking issue tagged `rollback,needs-triage`.
5. If `vars.AUTO_MERGE_ROLLBACKS == 'true'`, the PR is squash-merged
   immediately (otherwise a human reviews + merges).

The healthcheck script (`.github/scripts/post-deploy-healthcheck.py`) is
stdlib-only, unit-tested (`tests/test_healthcheck_script.py`) and exits with
distinct codes:

| Exit | Meaning                                                |
|------|--------------------------------------------------------|
| 0    | healthy throughout the window                          |
| 1    | degraded for longer than `--grace`                     |
| 2    | failed-job rate exceeded `--max-error-rate`            |
| 3    | URL never returned 200                                 |

**Setup:**
- `vars.PROD_URL` (the public URL of your deployed webhook)
- `secrets.PROD_ADMIN_TOKEN` (admin token — for `/metrics` checks; optional)
- `vars.AUTO_MERGE_ROLLBACKS=true` to enable hands-free rollback (default: open
  PR only, human-confirm)

If `vars.PROD_URL` isn't configured, the workflow exits cleanly with a notice.
This means the four new workflows ship **safely off** until you opt in.

## Required GitHub repo configuration

```
Variables:
  HF_USER                      — your HF account / org
  HF_DASHBOARD_SPACE           — e.g. alice/gh-deepagent
  HF_ALLINONE_SPACE            — e.g. alice/gh-deepagent-demo
  PROD_URL                     — https://your-webhook.example.com
  AUTO_MERGE_ROLLBACKS         — "true" to enable hands-free rollback

Secrets:
  HF_TOKEN                     — Hugging Face write token
  PROD_ADMIN_TOKEN             — DEEPAGENT_ADMIN_TOKEN of prod (for /metrics polling)
```

Everything else is autoconfigured by the workflows themselves.

## What gets triggered when

| You do                              | What runs                                                |
|-------------------------------------|----------------------------------------------------------|
| Open a PR                           | `ci`, `conventional-pr`, `pr-preview-hf`                 |
| Push to PR branch                   | `ci`, `pr-preview-hf` (updates the preview)              |
| Close/merge a PR                    | `pr-preview-hf` deletes the preview Space                |
| Merge into `main`                   | `ci`, `release-please`, `deploy-hf-*`, `deploy-streamlit-cloud`, `publish-image` (sha-tagged), `rollback-on-failure` |
| Merge the release-please PR         | All of the above **plus** `publish-image` tagged with the version + cosign signature, GitHub Release created |
| Push a `v*.*.*` tag manually        | `publish-image` only                                     |

## Local dry-run

```bash
# 1. ci.yml — the heavy job
PYTHONPATH=src pytest -q
docker build -f deploy/huggingface/dashboard/Dockerfile -t ci:test .
docker build -f deploy/huggingface/all-in-one/Dockerfile -t ci:test-all .

# 2. Healthcheck script against a local instance
python .github/scripts/post-deploy-healthcheck.py \
  --url http://localhost:8080 \
  --admin-token "$DEEPAGENT_ADMIN_TOKEN" \
  --timeout 30 --grace 5 --max-error-rate 0.5
```

## Failure modes & their recovery

| Symptom                                             | Fix                                                   |
|-----------------------------------------------------|--------------------------------------------------------|
| `publish-image` fails: "name unknown"               | First push only — package visibility is per-user; trigger once with workflow_dispatch and accept the package. |
| `release-please` doesn't open a PR                  | No `feat`/`fix` commits since last release. Push a commit with one. |
| `pr-preview-hf` says "preview skipped"              | PR author isn't a collaborator → add `preview-ok` label. |
| `rollback-on-failure` revert PR has conflicts       | A new commit landed between the bad commit and the revert. Resolve manually. |
| `deploy-hf-*` fails with `403 Forbidden`            | HF_TOKEN expired or lacks write access to the Space.   |

## Why not all-in-one?

Each workflow has its own concurrency group, permissions, and failure mode.
Splitting them means:

- a docker build failure doesn't block release-please from cutting a version
- a PR preview can crash without affecting `ci`
- rollback runs in a fresh job with no inherited state from the deploy

10 small workflows beat 1 monolithic file every time.
