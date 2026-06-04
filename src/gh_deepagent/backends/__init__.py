"""Backend factory — chooses between Local, Daytona, Modal, Runloop sandboxes.

Selection is driven by the env var DEEPAGENT_BACKEND:
  - "local"   (default) → LocalShellBackend rooted in the cloned repo
  - "daytona"           → DaytonaSandbox (remote, isolated)
  - "modal"             → ModalSandbox   (remote, isolated)
  - "runloop"           → RunloopSandbox (remote, isolated)

LAYERED BACKEND (NEW):
    When env DEEPAGENT_LAYERED_MEMORY=1, the local backend is wrapped in a
    CompositeBackend that routes:

        /                  → LocalShellBackend   (per-job, ephemeral)
        /memories/<repo>/  → StoreBackend         (persistent across jobs of
                                                   the same repo)

    The agent sees a single virtual filesystem; subsequent jobs on the same
    repo can read what previous jobs wrote under /memories/<repo>/. Useful for
    remembering repo conventions, past decisions, "do not touch this file"
    notes, etc.

For remote sandboxes, the cloned repo is uploaded into the sandbox before the
agent starts, and pulled back when finalize_patch needs to push. This keeps the
GitHub side-effects (commit/push/PR) on the host machine, which holds GITHUB_TOKEN.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional, Protocol



def _layered_memory_enabled() -> bool:
    return os.getenv("DEEPAGENT_LAYERED_MEMORY", "0").lower() in ("1", "true", "yes")


def _slugify_repo(repo_full_name: str) -> str:
    """org/repo → org__repo, safe in a filesystem path."""
    return repo_full_name.replace("/", "__").replace(" ", "_")


# ---------------------------------------------------------------- factory


class BackendHandle(Protocol):
    """What the rest of the app needs from a backend."""
    backend: Any           # passed to create_deep_agent(backend=...)
    repo_path: Path        # absolute host path where the working copy lives
    is_remote: bool        # True if commits need an upload/download round-trip
    memory_path: Optional[str]   # NEW: the /memories/<repo>/ root, or None

    def cleanup(self) -> None: ...
    def sync_to_host(self) -> None: ...   # remote → host (no-op for local)
    def sync_from_host(self) -> None: ...  # host → remote (no-op for local)


# ---------------------------------------------------------------- helpers


def _maybe_wrap_layered(
    leaf_backend: Any,
    repo_full_name: Optional[str],
) -> tuple[Any, Optional[str]]:
    """Wrap the leaf backend in a CompositeBackend with /memories/<repo>/.

    Returns (final_backend, memory_path_or_none).

    No-op (leaf returned untouched) when:
        - DEEPAGENT_LAYERED_MEMORY is not set, OR
        - repo_full_name is None (e.g. legacy code path), OR
        - the optional deepagents imports aren't available (e.g. very old
          versions of deepagents — we degrade gracefully).
    """
    if not _layered_memory_enabled() or repo_full_name is None:
        return leaf_backend, None

    try:
        from deepagents.backends import CompositeBackend, StoreBackend
    except ImportError:
        # Older deepagents → no CompositeBackend; skip silently.
        return leaf_backend, None

    repo_slug = _slugify_repo(repo_full_name)
    memory_prefix = f"/memories/{repo_slug}/"

    # The StoreBackend needs a `store` to be passed to create_deep_agent; we
    # build it lazily via a callable so the caller can wire an InMemoryStore
    # or a real LangGraph store.
    try:
        store_backend = StoreBackend(namespace=lambda _rt: ("memories", repo_slug))
    except TypeError:
        # Very old StoreBackend signature without namespace factory.
        store_backend = StoreBackend()

    composite = CompositeBackend(
        default=leaf_backend,
        routes={memory_prefix: store_backend},
    )
    return composite, memory_prefix


# ---------------------------------------------------------------- handles


class _LocalHandle:
    is_remote = False

    def __init__(self, repo_path: Path, repo_full_name: Optional[str] = None):
        self.repo_path = repo_path
        try:
            from deepagents.backends import LocalShellBackend
            leaf = LocalShellBackend(
                root_dir=str(repo_path.resolve()),
                env={"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"},
            )
        except ImportError:
            # Test/CI environments without deepagents installed get a sentinel
            # leaf object so we can still validate the layered-wrap logic.
            leaf = object()
        self.backend, self.memory_path = _maybe_wrap_layered(leaf, repo_full_name)

    def cleanup(self) -> None: ...
    def sync_to_host(self) -> None: ...
    def sync_from_host(self) -> None: ...


def _walk_files(root: Path):
    for p in root.rglob("*"):
        if p.is_file() and ".git" not in p.parts:
            yield p


class _DaytonaHandle:
    is_remote = True

    def __init__(self, repo_path: Path, repo_full_name: Optional[str] = None):
        from daytona import Daytona              # type: ignore
        from langchain_daytona import DaytonaSandbox  # type: ignore

        self.repo_path = repo_path
        self._sandbox = Daytona().create()
        leaf = DaytonaSandbox(sandbox=self._sandbox)
        self.backend, self.memory_path = _maybe_wrap_layered(leaf, repo_full_name)
        self._leaf = leaf
        self._remote_root = "/workspace/repo"
        self.sync_from_host()

    def sync_from_host(self) -> None:
        files = [
            (f"{self._remote_root}/{p.relative_to(self.repo_path)}", p.read_bytes())
            for p in _walk_files(self.repo_path)
        ]
        for i in range(0, len(files), 50):
            self._leaf.upload_files(files[i : i + 50])
        self._leaf.execute(f"mkdir -p {self._remote_root} && cd {self._remote_root}")

    def sync_to_host(self) -> None:
        listing = self._leaf.execute(f"find {self._remote_root} -type f -not -path '*/.git/*'")
        paths = [ln.strip() for ln in (listing.output or "").splitlines() if ln.strip()]
        if not paths:
            return
        results = self._leaf.download_files(paths)
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

    def __init__(self, repo_path: Path, repo_full_name: Optional[str] = None):
        import modal                              # type: ignore
        from langchain_modal import ModalSandbox  # type: ignore

        self.repo_path = repo_path
        app_name = os.getenv("DEEPAGENT_MODAL_APP", "gh-deepagent")
        image_name = os.getenv("DEEPAGENT_MODAL_IMAGE", "python:3.12-slim")
        app = modal.App.lookup(app_name, create_if_missing=True)
        self._sandbox = modal.Sandbox.create(image=modal.Image.from_registry(image_name), app=app)
        leaf = ModalSandbox(sandbox=self._sandbox)
        self.backend, self.memory_path = _maybe_wrap_layered(leaf, repo_full_name)
        self._leaf = leaf
        self._remote_root = "/workspace/repo"
        self.sync_from_host()

    def sync_from_host(self) -> None:
        files = [
            (f"{self._remote_root}/{p.relative_to(self.repo_path)}", p.read_bytes())
            for p in _walk_files(self.repo_path)
        ]
        for i in range(0, len(files), 50):
            self._leaf.upload_files(files[i : i + 50])

    def sync_to_host(self) -> None:
        listing = self._leaf.execute(f"find {self._remote_root} -type f -not -path '*/.git/*'")
        paths = [ln.strip() for ln in (listing.output or "").splitlines() if ln.strip()]
        if not paths:
            return
        results = self._leaf.download_files(paths)
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

    def __init__(self, repo_path: Path, repo_full_name: Optional[str] = None):
        from runloop_api_client import RunloopSDK     # type: ignore
        from langchain_runloop import RunloopSandbox  # type: ignore

        api_key = os.environ["RUNLOOP_API_KEY"]
        client = RunloopSDK(bearer_token=api_key)
        self._devbox = client.devbox.create()
        leaf = RunloopSandbox(devbox=self._devbox)
        self.backend, self.memory_path = _maybe_wrap_layered(leaf, repo_full_name)
        self._leaf = leaf
        self.repo_path = repo_path
        self._remote_root = "/workspace/repo"
        self.sync_from_host()

    def sync_from_host(self) -> None:
        files = [
            (f"{self._remote_root}/{p.relative_to(self.repo_path)}", p.read_bytes())
            for p in _walk_files(self.repo_path)
        ]
        for i in range(0, len(files), 50):
            self._leaf.upload_files(files[i : i + 50])

    def sync_to_host(self) -> None:
        listing = self._leaf.execute(f"find {self._remote_root} -type f -not -path '*/.git/*'")
        paths = [ln.strip() for ln in (listing.output or "").splitlines() if ln.strip()]
        if not paths:
            return
        results = self._leaf.download_files(paths)
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


def get_backend_handle(
    repo_path: Path,
    kind: Optional[str] = None,
    repo_full_name: Optional[str] = None,
) -> BackendHandle:
    kind = (kind or os.getenv("DEEPAGENT_BACKEND") or "local").lower()
    if kind not in _BACKENDS:
        raise ValueError(f"Unknown DEEPAGENT_BACKEND={kind}. Choices: {list(_BACKENDS)}")
    return _BACKENDS[kind](repo_path, repo_full_name=repo_full_name)  # type: ignore[return-value]
