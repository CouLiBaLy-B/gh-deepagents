"""High-level orchestration: fix-issue, evolve-code, iterate-pr, review-pr.

All entrypoints converge here so that the CLI, the GitHub Actions glue script and
the webhook server share the same code path.
"""
from __future__ import annotations

import json
import subprocess
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console

from .agent import build_agent
from .config import get_settings
from .github_client import GitHubOps, IssueRef, normalize_repo_full_name

console = Console()


@dataclass
class RunResult:
    ok: bool
    summary: str
    pr_url: Optional[str] = None
    diff: Optional[str] = None


def _stream(agent, initial_input: dict, max_turns: int) -> str:
    """Stream the agent, log structured events, observe sub-agent calls.

    Also publishes log lines to Redis pub/sub when running inside a job
    (so SSE subscribers see live progress).
    """
    from .observability.metrics import SUBAGENT_CALLS
    from .observability.logging_setup import get_logger
    from .observability.tracing import span

    logger = get_logger("agent.stream")
    final_text = ""
    turns = 0
    seen_subagents: set[str] = set()

    # If we're inside a job, attach a stream sink so SSE clients see live output.
    try:
        import structlog
        ctx = structlog.contextvars.get_contextvars()
    except Exception:
        ctx = {}
    job_id = ctx.get("job_id") if isinstance(ctx, dict) else None
    job_queue = None
    if job_id:
        try:
            from .queue import JobQueue
            job_queue = JobQueue()
        except Exception:
            job_queue = None

    with span("agent.stream", max_turns=max_turns):
        for chunk in agent.stream(initial_input, config={"recursion_limit": max_turns * 4}):
            turns += 1
            for node, payload in chunk.items():
                if node not in seen_subagents and node not in {"agent", "tools", "__start__", "__end__"}:
                    SUBAGENT_CALLS.labels(node).inc()
                    seen_subagents.add(node)
                if "messages" in payload:
                    msg = payload["messages"][-1]
                    role = getattr(msg, "type", "?")
                    content = getattr(msg, "content", "") or ""
                    line = f"[{node}] {role}: {str(content)[:400]}"
                    console.print(f"[dim]{node}[/] [{role}] {str(content)[:400]}")
                    logger.info("agent_step", agent_node=node, role=role,
                                content_preview=str(content)[:200])
                    if job_queue and job_id:
                        try: job_queue.append_log(job_id, line)
                        except Exception: pass
                    if role == "ai" and content:
                        final_text = content
            if turns >= max_turns:
                console.print("[yellow]Hit max_turns, stopping.[/]")
                logger.warning("max turns hit", turns=turns)
                break
    return final_text


def _extract_pr_url(text: str) -> Optional[str]:
    import re
    m = re.search(r"https://github\.com/[^/]+/[^/]+/pull/\d+", text or "")
    return m.group(0) if m else None


def _dry_run_result(repo_path: Path, text: str) -> RunResult:
    diff = subprocess.run(
        ["git", "diff", "HEAD"], cwd=repo_path, capture_output=True, text=True
    ).stdout
    return RunResult(ok=True, summary=text, diff=diff)


# ---------- public entrypoints ----------

def fix_issue(issue_url: str, dry_run: bool = False, backend: Optional[str] = None) -> RunResult:
    """Resolve a GitHub issue and (unless dry_run) open a PR."""
    settings = get_settings()
    settings.assert_ready()

    ref = IssueRef.from_url(issue_url)
    gh = GitHubOps()
    ctx = gh.fetch_issue_context(ref)

    console.rule(f"[bold green]Issue #{ref.number} — {ctx['title']}")
    repo_path = gh.clone(ref.full_name, settings.workdir)
    console.print(f"[cyan]Cloned[/] {ref.full_name} → {repo_path}")

    agent, handle = build_agent(
        repo_path=repo_path, repo_full_name=ref.full_name, issue_ref=ref, backend_kind=backend
    )
    try:
        prompt = textwrap.dedent(f"""\
            Resolve GitHub issue #{ref.number} in repo {ref.full_name}.

            Title: {ctx['title']}

            Body:
            {ctx['body']}

            Comments (chronological):
            {json.dumps(ctx['comments'], indent=2, default=str)}

            Working directory contains a fresh clone of the repo. Investigate, plan, patch,
            test, then {"DO NOT open a PR — print the diff and stop." if dry_run else f"call finalize_patch with branch name `deepagent/issue-{ref.number}`."}
        """)

        text = _stream(agent, {"messages": [{"role": "user", "content": prompt}]}, settings.max_turns)
        if dry_run:
            return _dry_run_result(repo_path, text)
        return RunResult(ok=bool(_extract_pr_url(text)), summary=text, pr_url=_extract_pr_url(text))
    finally:
        handle.cleanup()


def evolve_code(
    repo_full_name: str,
    instruction: str,
    dry_run: bool = False,
    backend: Optional[str] = None,
) -> RunResult:
    """Apply a free-form evolution request to the repo."""
    repo_full_name = normalize_repo_full_name(repo_full_name)
    settings = get_settings()
    settings.assert_ready()

    gh = GitHubOps()
    console.rule(f"[bold green]Evolve {repo_full_name}")
    repo_path = gh.clone(repo_full_name, settings.workdir)
    console.print(f"[cyan]Cloned[/] {repo_full_name} → {repo_path}")

    agent, handle = build_agent(
        repo_path=repo_path, repo_full_name=repo_full_name, backend_kind=backend
    )
    slug = f"evolve-{int(time.time())}"
    try:
        prompt = textwrap.dedent(f"""\
            Evolution request for repo {repo_full_name}:

            \"\"\"
            {instruction}
            \"\"\"

            Investigate the codebase, build a minimal plan, implement the change, run the
            tests, and {"print the diff (no PR)." if dry_run else f"call finalize_patch with branch name `deepagent/{slug}`."}
        """)
        text = _stream(agent, {"messages": [{"role": "user", "content": prompt}]}, settings.max_turns)
        if dry_run:
            return _dry_run_result(repo_path, text)
        return RunResult(ok=bool(_extract_pr_url(text)), summary=text, pr_url=_extract_pr_url(text))
    finally:
        handle.cleanup()


def iterate_pr(
    repo_full_name: str,
    pr_number: int,
    instruction: str,
    dry_run: bool = False,
    backend: Optional[str] = None,
) -> RunResult:
    """Push additional commits to the branch backing an existing PR.

    Triggered by `/deepagent <instruction>` on a PR comment, or via CLI.
    """
    repo_full_name = normalize_repo_full_name(repo_full_name)
    settings = get_settings()
    settings.assert_ready()
    gh = GitHubOps()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)
    head_branch = pr.head.ref
    base_branch = pr.base.ref

    console.rule(f"[bold green]Iterate on PR #{pr_number} — branch {head_branch}")
    repo_path = gh.clone(repo_full_name, settings.workdir)
    # Check out the PR branch so the agent sees the latest PR state.
    subprocess.run(
        ["git", "fetch", "origin", head_branch], cwd=repo_path, check=True
    )
    subprocess.run(["git", "checkout", head_branch], cwd=repo_path, check=True)

    agent, handle = build_agent(
        repo_path=repo_path,
        repo_full_name=repo_full_name,
        backend_kind=backend,
        base_branch=base_branch,
        existing_branch=head_branch,
    )
    try:
        # Give the agent the existing PR context so it understands what to amend.
        pr_diff = subprocess.run(
            ["git", "diff", f"origin/{base_branch}...HEAD"],
            cwd=repo_path, capture_output=True, text=True,
        ).stdout[:15000]

        prompt = textwrap.dedent(f"""\
            You are amending an EXISTING pull request.
            Repo: {repo_full_name}   PR: #{pr_number}   Branch: {head_branch}   Base: {base_branch}
            PR title: {pr.title}
            PR body (truncated):
            {(pr.body or '')[:2000]}

            Current PR diff vs base:
            ```diff
            {pr_diff}
            ```

            Reviewer instruction:
            \"\"\"
            {instruction}
            \"\"\"

            Make the requested changes ON TOP of the current branch. Run the tests.
            When done, {"print the diff and stop." if dry_run else "call finalize_patch (branch_name argument will be ignored — the existing branch is used)."}
        """)
        text = _stream(agent, {"messages": [{"role": "user", "content": prompt}]}, settings.max_turns)
        if dry_run:
            return _dry_run_result(repo_path, text)
        # Even if the LLM didn't echo a URL, return the existing PR URL.
        return RunResult(ok=True, summary=text, pr_url=pr.html_url)
    finally:
        handle.cleanup()


def review_pr(repo_full_name: str, pr_number: int, backend: Optional[str] = None) -> RunResult:
    """Post an automated code-review comment on a PR.

    Tries to render a structured ReviewReport from the reviewer sub-agent's
    response_format output; falls back to the agent's raw final message if the
    schema parse fails (older deepagents / unstructured run).
    """
    import urllib.request
    repo_full_name = normalize_repo_full_name(repo_full_name)
    settings = get_settings()
    settings.assert_ready()
    gh = GitHubOps()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)
    diff_text = urllib.request.urlopen(pr.diff_url, timeout=30).read().decode("utf-8", errors="replace")

    repo_path = gh.clone(repo_full_name, settings.workdir)
    agent, handle = build_agent(
        repo_path=repo_path, repo_full_name=repo_full_name, backend_kind=backend
    )
    try:
        prompt = (
            f"Review the following diff for PR #{pr_number} of {repo_full_name}.\n\n"
            f"Delegate to the `reviewer` sub-agent. Return its structured "
            f"ReviewReport (or the most precise markdown you can if structured "
            f"output isn't available).\n\n"
            f"```diff\n{diff_text[:30000]}\n```"
        )
        result = _stream_with_structured(
            agent,
            {"messages": [{"role": "user", "content": prompt}]},
            settings.max_turns,
        )
        body = _render_review_body(result)
        pr.create_issue_comment(body)
        return RunResult(ok=True, summary=body, pr_url=pr.html_url)
    finally:
        handle.cleanup()


def _stream_with_structured(agent, initial_input: dict, max_turns: int):
    """Like _stream() but also captures `structured_response` from the final state."""
    from .observability.logging_setup import get_logger
    logger = get_logger("agent.stream")

    final_text = ""
    structured = None
    turns = 0
    for chunk in agent.stream(initial_input, config={"recursion_limit": max_turns * 4}):
        turns += 1
        for node, payload in chunk.items():
            if isinstance(payload, dict):
                if "structured_response" in payload and payload["structured_response"] is not None:
                    structured = payload["structured_response"]
                if "messages" in payload:
                    msg = payload["messages"][-1]
                    content = getattr(msg, "content", "") or ""
                    if getattr(msg, "type", "?") == "ai" and content:
                        final_text = content
        if turns >= max_turns:
            logger.warning("max turns hit", turns=turns)
            break
    return {"text": final_text, "structured": structured}


def _render_review_body(result: dict) -> str:
    """Choose the prettiest rendering we can for the GitHub comment."""
    structured = result.get("structured")
    if structured is not None:
        try:
            from .review_schema import ReviewReport, render_report_markdown
            # `structured` might already be a ReviewReport (Pydantic), a dict,
            # or another model instance — normalise.
            if isinstance(structured, ReviewReport):
                return render_report_markdown(structured)
            if hasattr(structured, "model_dump"):
                return render_report_markdown(ReviewReport.model_validate(structured.model_dump()))
            if isinstance(structured, dict):
                return render_report_markdown(ReviewReport.model_validate(structured))
        except Exception:
            pass  # fall through to raw text
    text = result.get("text") or "(no review produced)"
    return f"### 🤖 gh-deepagent review\n\n{text}"
