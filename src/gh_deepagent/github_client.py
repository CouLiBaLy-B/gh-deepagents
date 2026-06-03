"""Thin wrapper around PyGithub + GitPython for the operations the agent needs.

Supports both PAT (single tenant) and GitHub App (multi-tenant). The auth lives in
`gh_deepagent.auth.GitHubCredentials`; this module is just plumbing.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from git import Repo
from github.Issue import Issue
from github.PullRequest import PullRequest
from github.Repository import Repository

from .auth import GitHubCredentials

ISSUE_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)"
)


@dataclass
class IssueRef:
    owner: str
    repo: str
    number: int

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @classmethod
    def from_url(cls, url: str) -> "IssueRef":
        m = ISSUE_URL_RE.search(url)
        if not m:
            raise ValueError(f"Not a github issue URL: {url}")
        return cls(owner=m["owner"], repo=m["repo"], number=int(m["number"]))


class GitHubOps:
    """All side-effects against GitHub & git live here so they can be unit-tested."""

    def __init__(self, creds: GitHubCredentials | None = None):
        # Default to the process-wide singleton so installation-id cache and
        # access-token cache are shared across requests inside the webhook.
        self._creds = creds or GitHubCredentials.shared()

    # ---------- read ----------
    def get_repo(self, full_name: str) -> Repository:
        return self._creds.client_for_repo(full_name).get_repo(full_name)

    def get_issue(self, ref: IssueRef) -> Issue:
        return self.get_repo(ref.full_name).get_issue(ref.number)

    def fetch_issue_context(self, ref: IssueRef) -> dict:
        issue = self.get_issue(ref)
        comments = [
            {"author": c.user.login, "body": c.body, "created_at": c.created_at.isoformat()}
            for c in issue.get_comments()
        ]
        return {
            "title": issue.title,
            "body": issue.body or "",
            "labels": [lbl.name for lbl in issue.labels],
            "state": issue.state,
            "comments": comments,
            "number": issue.number,
            "html_url": issue.html_url,
        }

    # ---------- clone / git ----------
    def clone(self, full_name: str, workdir: Path, depth: int = 1) -> Path:
        """Shallow clone using a fresh per-repo token so we can push later."""
        workdir.mkdir(parents=True, exist_ok=True)
        target = workdir / full_name.replace("/", "__")
        if target.exists():
            shutil.rmtree(target)
        token = self._creds.clone_token_for_repo(full_name)
        url = f"https://x-access-token:{token}@github.com/{full_name}.git"
        Repo.clone_from(url, target, depth=depth)
        # Persist token in the remote URL so subsequent `git push` works without env.
        # (Token rotates ~1h for App installs; if a push fails on 401, we refresh.)
        return target

    def refresh_remote(self, repo_path: Path, full_name: str) -> None:
        """Re-write the origin URL with a fresh token (App installs rotate every hour)."""
        token = self._creds.clone_token_for_repo(full_name)
        url = f"https://x-access-token:{token}@github.com/{full_name}.git"
        subprocess.run(
            ["git", "remote", "set-url", "origin", url],
            cwd=repo_path, check=True, capture_output=True,
        )

    def create_branch(self, repo_path: Path, branch: str) -> None:
        repo = Repo(repo_path)
        repo.git.checkout("HEAD", b=branch)

    def commit_all(
        self, repo_path: Path, message: str, author: str = "gh-deepagent <bot@deepagent>"
    ) -> bool:
        repo = Repo(repo_path)
        repo.git.add(A=True)
        if not repo.is_dirty(untracked_files=True):
            return False
        name, email = _parse_author(author)
        repo.git.commit("-m", message, author=f"{name} <{email}>")
        return True

    def push(self, repo_path: Path, branch: str, full_name: str | None = None) -> None:
        """Push, retrying once with a refreshed token if the first attempt 401's."""
        try:
            Repo(repo_path).git.push("origin", branch, set_upstream=True)
        except Exception as e:
            if full_name and "401" in str(e):
                self._creds.invalidate(full_name)
                self.refresh_remote(repo_path, full_name)
                Repo(repo_path).git.push("origin", branch, set_upstream=True)
            else:
                raise

    def diff(self, repo_path: Path, base: str = "HEAD~1") -> str:
        return subprocess.check_output(
            ["git", "diff", base], cwd=repo_path, text=True, stderr=subprocess.DEVNULL
        )

    # ---------- PR ----------
    def open_pr(
        self,
        full_name: str,
        head_branch: str,
        title: str,
        body: str,
        base: str = "main",
        draft: bool = False,
    ) -> PullRequest:
        repo = self.get_repo(full_name)
        try:
            return repo.create_pull(title=title, body=body, head=head_branch, base=base, draft=draft)
        except Exception:
            base = repo.default_branch
            return repo.create_pull(title=title, body=body, head=head_branch, base=base, draft=draft)

    def comment_issue(self, ref: IssueRef, body: str) -> None:
        self.get_issue(ref).create_comment(body)


def _parse_author(s: str) -> tuple[str, str]:
    m = re.match(r"(.*)<(.+)>", s.strip())
    if not m:
        return s.strip(), "bot@deepagent"
    return m.group(1).strip(), m.group(2).strip()
