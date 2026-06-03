"""Typer-based CLI."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax

from . import __version__
from .config import get_settings
from .runner import evolve_code, fix_issue, iterate_pr, review_pr

app = typer.Typer(
    name="gh-deepagent",
    help="Coding & GitHub-issue-solving agent built on LangChain Deep Agents.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()

BackendOpt = typer.Option(
    None,
    "--backend",
    "-b",
    help="Override DEEPAGENT_BACKEND. Choices: local, daytona, modal, runloop.",
)


@app.command()
def version():
    """Print the version."""
    console.print(f"gh-deepagent {__version__}")


@app.command()
def fix(
    issue_url: str = typer.Argument(..., help="Full GitHub issue URL."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Don't open a PR; print the diff."),
    backend: Optional[str] = BackendOpt,
):
    """Resolve a GitHub issue end-to-end → PR."""
    res = fix_issue(issue_url, dry_run=dry_run, backend=backend)
    _print_result(res)


@app.command()
def evolve(
    repo: str = typer.Option(None, "--repo", "-r", help="owner/name. Defaults to DEEPAGENT_DEFAULT_REPO."),
    instruction: str = typer.Option(..., "--instruction", "-i", help="What to change."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    backend: Optional[str] = BackendOpt,
):
    """Apply a free-form code evolution request → PR."""
    repo = repo or get_settings().default_repo
    if not repo:
        raise typer.BadParameter("Provide --repo or set DEEPAGENT_DEFAULT_REPO.")
    res = evolve_code(repo, instruction, dry_run=dry_run, backend=backend)
    _print_result(res)


@app.command()
def iterate(
    repo: str = typer.Option(..., "--repo", "-r"),
    pr: int = typer.Option(..., "--pr", help="PR number"),
    instruction: str = typer.Option(..., "--instruction", "-i"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    backend: Optional[str] = BackendOpt,
):
    """Iterate on an EXISTING PR (push more commits to its branch)."""
    res = iterate_pr(repo, pr, instruction, dry_run=dry_run, backend=backend)
    _print_result(res)


@app.command()
def review(
    repo: str = typer.Option(..., "--repo", "-r"),
    pr: int = typer.Option(..., "--pr", help="PR number"),
    backend: Optional[str] = BackendOpt,
):
    """Post an automated review comment on a PR."""
    res = review_pr(repo, pr, backend=backend)
    _print_result(res)


@app.command(name="app-info")
def app_info():
    """Print credentials mode + (if GitHub App) list installations."""
    from .auth import GitHubCredentials
    creds = GitHubCredentials.from_env()
    console.print(f"[bold]Auth mode:[/] {creds.mode}")
    if creds.is_app:
        gh = creds.for_app_metadata()
        try:
            app = gh.get_app()
            console.print(f"[bold]App:[/] {app.name} (id={app.id}, slug={app.slug})")
        except Exception as e:
            console.print(f"[yellow]Couldn't fetch app metadata: {e}")
        try:
            from github import GithubIntegration, Auth
            import os
            integration = GithubIntegration(
                auth=Auth.AppAuth(int(os.environ["DEEPAGENT_GITHUB_APP_ID"]),
                                  creds._private_key_pem)  # type: ignore[attr-defined]
            )
            console.print("[bold]Installations:[/]")
            for inst in integration.get_installations():
                account = inst.account.login if inst.account else "?"
                console.print(f"  • id={inst.id}  account={account}")
        except Exception as e:
            console.print(f"[yellow]Couldn't list installations: {e}")


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8080, "--port"),
):
    """Run the FastAPI webhook server (enqueues jobs in Redis)."""
    import uvicorn
    uvicorn.run("gh_deepagent.webhook.server:app", host=host, port=port, log_level="info")


@app.command()
def dashboard(
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8501, "--port"),
    api_url: str = typer.Option(
        None, "--api-url",
        help="gh-deepagent webhook URL (defaults to $DEEPAGENT_API_URL or http://localhost:8080).",
    ),
):
    """Launch the Streamlit admin dashboard."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    if api_url:
        os.environ["DEEPAGENT_API_URL"] = api_url

    app_path = Path(__file__).parent / "dashboard" / "app.py"
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(app_path),
        "--server.address", host,
        "--server.port", str(port),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]
    try:
        subprocess.run(cmd, check=False)
    except FileNotFoundError:
        console.print("[red]Streamlit is not installed. Run: pip install 'gh-deepagent[dashboard]'[/]")
        raise typer.Exit(1)


@app.command()
def worker(
    workers: int = typer.Option(1, "--workers", "-w", min=1, max=32,
                                 help="Number of worker threads in this process."),
    worker_id: str = typer.Option(None, "--id"),
):
    """Run worker(s) that drain the Redis queue and execute the agent.

    Scale horizontally by running multiple `gh-deepagent worker` processes
    (e.g. one per machine, or N replicas in k8s).
    """
    import threading
    from .observability import setup_observability
    from .queue import Worker

    setup_observability()
    if workers == 1:
        Worker(worker_id=worker_id).run()
        return
    threads = []
    for i in range(workers):
        wid = f"{worker_id or 'w'}-{i}"
        t = threading.Thread(target=Worker(worker_id=wid).run, name=wid, daemon=False)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()


queue_app = typer.Typer(help="Queue admin commands.")
app.add_typer(queue_app, name="queue")


@queue_app.command("stats")
def queue_stats():
    """Show queue depth and DLQ size."""
    from .queue import JobQueue
    q = JobQueue()
    if not q.ping():
        console.print("[red]Redis unreachable.[/]")
        raise typer.Exit(1)
    s = q.stats()
    console.print(f"[bold]Queue depth:[/] {s['queue_depth']}")
    console.print(f"[bold]DLQ size:[/]    {s['dead_letter']}")


@queue_app.command("dlq")
def queue_dlq(limit: int = 50):
    """List dead-letter jobs."""
    from .queue import JobQueue
    q = JobQueue()
    for j in q.list_dead(limit=limit):
        console.print(f"[red]{j.id}[/] {j.event:18s} {j.repo_full_name:40s} attempts={j.attempts}  err={j.error}")


@queue_app.command("requeue")
def queue_requeue(job_id: str):
    """Move a DLQ job back into the pending queue."""
    from .queue import JobQueue
    q = JobQueue()
    if q.requeue_dead(job_id):
        console.print(f"[green]requeued {job_id}[/]")
    else:
        console.print(f"[red]job {job_id} not found in DLQ[/]")
        raise typer.Exit(1)


@queue_app.command("show")
def queue_show(job_id: str):
    """Show a job's metadata + last log lines."""
    from .queue import JobQueue
    from dataclasses import asdict
    q = JobQueue()
    job = q.get(job_id)
    if not job:
        console.print(f"[red]job {job_id} not found[/]")
        raise typer.Exit(1)
    d = asdict(job)
    d["status"] = job.status.value
    console.print_json(data=d)
    console.rule("[bold]logs (tail 50)")
    for line in q.get_logs(job_id, tail=50):
        console.print(line)


@app.command(name="github-event")
def github_event(
    event_path: str = typer.Option(..., "--event-path", envvar="GITHUB_EVENT_PATH"),
    event_name: str = typer.Option(..., "--event-name", envvar="GITHUB_EVENT_NAME"),
    backend: Optional[str] = BackendOpt,
):
    """Dispatch a GitHub Actions event (issues / issue_comment / workflow_dispatch)."""
    settings = get_settings()
    payload = json.loads(Path(event_path).read_text())
    repo_full = payload["repository"]["full_name"]

    if event_name == "issues":
        action = payload.get("action")
        labels = [lbl["name"] for lbl in payload["issue"].get("labels", [])]
        if action in ("labeled", "opened") and (
            (action == "labeled" and payload["label"]["name"] == settings.trigger_label)
            or (action == "opened" and settings.trigger_label in labels)
        ):
            _print_result(fix_issue(payload["issue"]["html_url"], backend=backend))
            return
        console.print(f"[yellow]Issue event ignored (action={action}, labels={labels})")
        return

    if event_name == "issue_comment":
        action = payload.get("action")
        body = (payload["comment"]["body"] or "").strip()
        if action != "created" or not body.startswith(settings.command_prefix):
            console.print("[yellow]Comment doesn't start with command prefix; ignoring.")
            return
        instruction = body[len(settings.command_prefix):].strip()
        is_pr = "pull_request" in payload["issue"]
        if is_pr:
            pr_number = payload["issue"]["number"]
            if instruction.lower().startswith("review"):
                _print_result(review_pr(repo_full, pr_number, backend=backend))
                return
            # default on PR comments: iterate on the PR branch
            _print_result(iterate_pr(repo_full, pr_number, instruction, backend=backend))
            return
        # comment on a regular issue → evolution / fix
        _print_result(evolve_code(repo_full, instruction, backend=backend))
        return

    if event_name == "pull_request":
        action = payload.get("action")
        labels = [lbl["name"] for lbl in payload["pull_request"].get("labels", [])]
        if action in ("labeled", "opened") and settings.review_label in labels:
            _print_result(review_pr(repo_full, payload["pull_request"]["number"], backend=backend))
            return
        console.print(f"[yellow]PR event ignored (action={action}, labels={labels})")
        return

    if event_name == "workflow_dispatch":
        inputs = payload.get("inputs") or {}
        if inputs.get("issue_url"):
            _print_result(fix_issue(inputs["issue_url"], backend=backend))
        elif inputs.get("pr_number") and inputs.get("instruction"):
            _print_result(iterate_pr(repo_full, int(inputs["pr_number"]), inputs["instruction"], backend=backend))
        elif inputs.get("instruction"):
            _print_result(evolve_code(repo_full, inputs["instruction"], backend=backend))
        else:
            console.print("[red]workflow_dispatch needs issue_url, instruction, or pr_number+instruction.")
        return

    console.print(f"[red]Unsupported event: {event_name}")


def _print_result(res):
    console.rule("[bold]Result")
    if res.pr_url:
        console.print(f"[bold green]PR:[/] {res.pr_url}")
    console.print(Markdown(res.summary or "(no summary)"))
    if res.diff:
        console.rule("[bold]Diff (dry-run)")
        console.print(Syntax(res.diff[:8000], "diff", theme="ansi_dark"))


if __name__ == "__main__":
    app()
