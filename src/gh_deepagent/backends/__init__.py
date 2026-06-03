"""Backend factory — chooses between LocalShell, Daytona, Modal, Runloop sandboxes.

Selection is driven by the env var DEEPAGENT_BACKEND:
  - "local"   (default) → LocalShellBackend rooted in the cloned repo
  - "daytona"           → DaytonaSandbox (remote, isolated)
  - "modal"             → ModalSandbox   (remote, isolated)
  - "runloop"           → RunloopSandbox (remote, isolated)

For remote sandboxes, the cloned repo is uploaded into the sandbox before the
agent starts, and pulled back when finalize_patch needs to push. This keeps the
GitHub side-effects (commit/push/PR) on the host machine, which holds GITHUB_TOKEN.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol

from deepagents.backends import LocalShellBackend


class BackendHandle(Protocol):
    """What the rest of the app needs from a backend."""
    backend: Any           # passed to create_deep_agent(backend=...)
    repo_path: Path        # absolute host path where the working copy lives
    is_remote: bool        # True if commits need an upload/download round-trip
    def cleanup(self) -> None: ...
    def sync_to_host(self) -> None: ...   # remote → host (no-op for local)
    def sync_from_host(self) -> None: ...  # host → remote (no-op for local)


class _LocalHandle:
    is_remote = False

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self.backend = LocalShellBackend(
            root_dir=str(repo_path.resolve()),
            env={"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"},
        )

    def cleanup(self) -> None: ...
    def sync_to_host(self) -> None: ...
    def sync_from_host(self) -> None: ...


def _walk_files(root: Path):
    for p in root.rglob("*"):
        if p.is_file() and ".git" not in p.parts:
            yield p


class _DaytonaHandle:
    is_remote = True

    def __init__(self, repo_path: Path):
        from daytona import Daytona              # type: ignore
        from langchain_daytona import DaytonaSandbox  # type: ignore

        self.repo_path = repo_path
        self._sandbox = Daytona().create()
        self.backend = DaytonaSandbox(sandbox=self._sandbox)
        self._remote_root = "/workspace/repo"
        self.sync_from_host()

    def sync_from_host(self) -> None:
        files = [
            (f"{self._remote_root}/{p.relative_to(self.repo_path)}", p.read_bytes())
            for p in _walk_files(self.repo_path)
        ]
        # chunk uploads to avoid huge requests
        for i in range(0, len(files), 50):
            self.backend.upload_files(files[i : i + 50])
        # change agent's working dir inside the sandbox by exporting a marker file
        self.backend.execute(f"mkdir -p {self._remote_root} && cd {self._remote_root}")

    def sync_to_host(self) -> None:
        # Pull every file under /workspace/repo back; identical layout.
        listing = self.backend.execute(f"find {self._remote_root} -type f -not -path '*/.git/*'")
        paths = [ln.strip() for ln in (listing.output or "").splitlines() if ln.strip()]
        if not paths:
            return
        results = self.backend.download_files(paths)
        for r in results:
            if r.content is None:
                continue
            rel = r.path.removeprefix(self._remote_root + "/")
            target = self.repo_path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(r.content)

    def cleanup(self) -> None:
        try:
            self._sandbox.stop()
        except Exception:
            pass


class _ModalHandle:
    is_remote = True

    def __init__(self, repo_path: Path):
        import modal                              # type: ignore
        from langchain_modal import ModalSandbox  # type: ignore

        self.repo_path = repo_path
        app_name = os.getenv("DEEPAGENT_MODAL_APP", "gh-deepagent")
        image_name = os.getenv("DEEPAGENT_MODAL_IMAGE", "python:3.12-slim")
        app = modal.App.lookup(app_name, create_if_missing=True)
        self._sandbox = modal.Sandbox.create(image=modal.Image.from_registry(image_name), app=app)
        self.backend = ModalSandbox(sandbox=self._sandbox)
        self._remote_root = "/workspace/repo"
        self.sync_from_host()

    def sync_from_host(self) -> None:
        files = [
            (f"{self._remote_root}/{p.relative_to(self.repo_path)}", p.read_bytes())
            for p in _walk_files(self.repo_path)
        ]
        for i in range(0, len(files), 50):
            self.backend.upload_files(files[i : i + 50])

    def sync_to_host(self) -> None:
        listing = self.backend.execute(f"find {self._remote_root} -type f -not -path '*/.git/*'")
        paths = [ln.strip() for ln in (listing.output or "").splitlines() if ln.strip()]
        if not paths:
            return
        results = self.backend.download_files(paths)
        for r in results:
            if r.content is None:
                continue
            rel = r.path.removeprefix(self._remote_root + "/")
            target = self.repo_path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(r.content)

    def cleanup(self) -> None:
        try:
            self._sandbox.terminate()
        except Exception:
            pass


class _RunloopHandle:
    is_remote = True

    def __init__(self, repo_path: Path):
        from runloop_api_client import RunloopSDK     # type: ignore
        from langchain_runloop import RunloopSandbox  # type: ignore

        api_key = os.environ["RUNLOOP_API_KEY"]
        client = RunloopSDK(bearer_token=api_key)
        self._devbox = client.devbox.create()
        self.backend = RunloopSandbox(devbox=self._devbox)
        self.repo_path = repo_path
        self._remote_root = "/workspace/repo"
        self.sync_from_host()

    def sync_from_host(self) -> None:
        files = [
            (f"{self._remote_root}/{p.relative_to(self.repo_path)}", p.read_bytes())
            for p in _walk_files(self.repo_path)
        ]
        for i in range(0, len(files), 50):
            self.backend.upload_files(files[i : i + 50])

    def sync_to_host(self) -> None:
        listing = self.backend.execute(f"find {self._remote_root} -type f -not -path '*/.git/*'")
        paths = [ln.strip() for ln in (listing.output or "").splitlines() if ln.strip()]
        if not paths:
            return
        results = self.backend.download_files(paths)
        for r in results:
            if r.content is None:
                continue
            rel = r.path.removeprefix(self._remote_root + "/")
            target = self.repo_path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(r.content)

    def cleanup(self) -> None:
        try:
            self._devbox.delete()
        except Exception:
            pass


_BACKENDS = {
    "local": _LocalHandle,
    "daytona": _DaytonaHandle,
    "modal": _ModalHandle,
    "runloop": _RunloopHandle,
}


def get_backend_handle(repo_path: Path, kind: str | None = None) -> BackendHandle:
    kind = (kind or os.getenv("DEEPAGENT_BACKEND") or "local").lower()
    if kind not in _BACKENDS:
        raise ValueError(f"Unknown DEEPAGENT_BACKEND={kind}. Choices: {list(_BACKENDS)}")
    return _BACKENDS[kind](repo_path)  # type: ignore[return-value]
