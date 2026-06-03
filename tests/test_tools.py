"""Tests for the Toolbox, role splitting, and language auto-detection."""
from __future__ import annotations

from pathlib import Path

import pytest

from gh_deepagent.tools import (
    Toolbox,
    _autodetect_audit_cmd,
    _autodetect_fmt_cmd,
    _autodetect_lint_cmd,
    _autodetect_test_cmd,
    _detect_runner,
    make_toolbox,
)


@pytest.fixture()
def fake_repo(tmp_path: Path, monkeypatch) -> Path:
    """A minimal Python repo skeleton."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("def f(): return 1\n")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    return tmp_path


def test_toolbox_segmentation(fake_repo):
    tb = make_toolbox(repo_path=fake_repo, repo_full_name="o/r")
    names = lambda lst: {t.name for t in lst}
    assert "finalize_patch" in names(tb.finalize)
    assert "finalize_patch" not in names(tb.read_only)
    assert "finalize_patch" not in names(tb.edit)
    # Read-only never includes shell-mutating tools
    assert "run_tests" not in names(tb.read_only)
    assert "lint_check" not in names(tb.read_only)
    # All shell-edit tools live in edit
    assert {"run_tests", "lint_check", "format_code", "dependency_audit", "scan_secrets"} <= names(tb.edit)


def test_for_role_least_privilege(fake_repo):
    tb = make_toolbox(repo_path=fake_repo, repo_full_name="o/r")
    # Reviewer / planner / security never get finalize
    for role in ("planner", "reviewer", "security"):
        assert "finalize_patch" not in {t.name for t in tb.for_role(role)}
    # Coder gets edit but not finalize
    coder = {t.name for t in tb.for_role("coder")}
    assert "lint_check" in coder
    assert "finalize_patch" not in coder
    # Only lead gets finalize
    assert "finalize_patch" in {t.name for t in tb.for_role("lead")}


def test_for_role_unknown_raises(fake_repo):
    tb = make_toolbox(repo_path=fake_repo, repo_full_name="o/r")
    with pytest.raises(ValueError):
        tb.for_role("evil-overlord")


# ---------- autodetectors ----------

def test_detect_python(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert _autodetect_test_cmd(tmp_path) == "pytest -q"
    assert any("ruff check" in c for c in _autodetect_lint_cmd(tmp_path))
    assert _autodetect_fmt_cmd(tmp_path) == "ruff format ."
    assert "pip-audit" in _autodetect_audit_cmd(tmp_path)


def test_detect_node(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name":"x"}')
    assert "npm test" in _autodetect_test_cmd(tmp_path)
    assert any("eslint" in c for c in _autodetect_lint_cmd(tmp_path))
    assert "prettier" in _autodetect_fmt_cmd(tmp_path)
    assert "npm audit" in _autodetect_audit_cmd(tmp_path)


def test_detect_go(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module x\n")
    assert _autodetect_test_cmd(tmp_path) == "go test ./..."
    assert any("golangci-lint" in c for c in _autodetect_lint_cmd(tmp_path))
    assert _autodetect_fmt_cmd(tmp_path) == "gofmt -w ."


def test_detect_rust(tmp_path: Path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    assert _autodetect_test_cmd(tmp_path) == "cargo test --quiet"
    assert any("cargo clippy" in c for c in _autodetect_lint_cmd(tmp_path))
    assert "cargo audit" in _autodetect_audit_cmd(tmp_path)


def test_detect_runner_strings():
    assert _detect_runner("==== 1 failed, 2 passed in 0.1s") == "pytest"
    assert _detect_runner("PASS  src/foo.test.ts") == "jest"
    assert _detect_runner("ok\tgithub.com/x/y\t0.001s") == "go test"
    assert _detect_runner("running 3 tests\ntest result: ok") == "cargo test"
    assert _detect_runner("hello") == "unknown"


# ---------- analyze_test_failure ----------

def test_analyze_pytest_failure(fake_repo):
    tb = make_toolbox(repo_path=fake_repo, repo_full_name="o/r")
    analyze = next(t for t in tb.read_only if t.name == "analyze_test_failure")
    sample = """
______________________________ test_foo ______________________________

    def test_foo():
>       assert 1 == 2
E       assert 1 == 2

tests/test_foo.py:3: AssertionError
=================== 1 failed, 0 passed in 0.01s ====================
"""
    import json
    out = json.loads(analyze.invoke({"test_output": sample}))
    assert out["runner"] == "pytest"
    assert out["failed_count"] == 1
    assert len(out["failed"]) == 1
    f = out["failed"][0]
    assert f["name"] == "test_foo"
    assert f["file"] == "tests/test_foo.py"
    assert f["line"] == 3


# ---------- finalize_patch safety net ----------

def test_finalize_refuses_main_branch(fake_repo):
    tb = make_toolbox(repo_path=fake_repo, repo_full_name="o/r")
    finalize = tb.finalize[0]
    out = finalize.invoke({
        "branch_name": "main",
        "commit_message": "x",
        "pr_title": "t",
        "pr_body": "b",
    })
    assert "REFUSED" in out
