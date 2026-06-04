"""Layered-memory CompositeBackend wrapping."""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from gh_deepagent.backends import (
    _LocalHandle,
    _maybe_wrap_layered,
    _slugify_repo,
    get_backend_handle,
)


def test_slugify():
    assert _slugify_repo("org/repo") == "org__repo"
    assert _slugify_repo("My Org/My Repo") == "My_Org__My_Repo"


def test_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPAGENT_LAYERED_MEMORY", raising=False)
    h = _LocalHandle(tmp_path, repo_full_name="org/repo")
    assert h.memory_path is None
    # backend stays the leaf (not composite)
    assert "Composite" not in type(h.backend).__name__


def test_enabled_wraps_into_composite(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPAGENT_LAYERED_MEMORY", "1")
    h = _LocalHandle(tmp_path, repo_full_name="org/repo")
    # If CompositeBackend isn't importable in this env we degrade gracefully.
    if h.memory_path is None:
        pytest.skip("deepagents CompositeBackend not available in this env")
    assert h.memory_path == "/memories/org__repo/"
    assert "Composite" in type(h.backend).__name__


def test_no_wrap_without_repo_name(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPAGENT_LAYERED_MEMORY", "1")
    h = _LocalHandle(tmp_path, repo_full_name=None)
    assert h.memory_path is None


def test_factory_dispatch_local(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPAGENT_LAYERED_MEMORY", raising=False)
    h = get_backend_handle(tmp_path, kind="local", repo_full_name="o/r")
    assert h.is_remote is False
    assert h.repo_path == tmp_path


def test_factory_rejects_unknown(tmp_path):
    with pytest.raises(ValueError):
        get_backend_handle(tmp_path, kind="madeup")
