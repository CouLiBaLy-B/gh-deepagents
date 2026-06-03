"""Tests for migrator / perf-analyst / i18n tools and their role wiring."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gh_deepagent.tools import make_toolbox


@pytest.fixture()
def py_repo(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f():\n    return 1\n")
    (tmp_path / "src" / "b.py").write_text("def f():\n    return 2\n")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    return tmp_path


@pytest.fixture()
def i18n_repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    locales = tmp_path / "locales"
    locales.mkdir()
    (locales / "en.json").write_text(json.dumps({
        "hello": "Hello",
        "nested": {"a": "A", "b": "B"},
        "only_en": "only in en",
    }, indent=2))
    (locales / "fr.json").write_text(json.dumps({
        "hello": "Bonjour",
        "nested": {"a": "A_fr"},          # missing nested.b
        "only_fr": "extra",                # extra key
    }, indent=2))
    return tmp_path


# ============================================================== ROLES

def test_new_roles_get_their_tools(py_repo):
    tb = make_toolbox(repo_path=py_repo, repo_full_name="o/r")
    names = lambda lst: {t.name for t in lst}

    migrator = names(tb.for_role("migrator"))
    assert {"ast_grep_search", "ast_grep_rewrite", "codemod_python"} <= migrator
    assert "run_tests" in migrator                     # inherits edit
    assert "finalize_patch" not in migrator            # but not finalize

    perf = names(tb.for_role("perf-analyst"))
    assert {"benchmark_run", "profile_python", "cprofile_run", "perf_compare"} <= perf
    assert "run_tests" in perf                          # explicitly allowed
    assert "lint_check" not in perf                     # other edit tools blocked
    assert "format_code" not in perf
    assert "finalize_patch" not in perf

    i18n = names(tb.for_role("i18n"))
    assert {"i18n_list_locales", "i18n_extract", "i18n_check_parity"} <= i18n
    assert "finalize_patch" not in i18n


def test_lead_gets_everything(py_repo):
    tb = make_toolbox(repo_path=py_repo, repo_full_name="o/r")
    lead = {t.name for t in tb.for_role("lead")}
    for tname in (
        "ast_grep_search", "ast_grep_rewrite", "codemod_python",
        "benchmark_run", "profile_python", "perf_compare",
        "i18n_list_locales", "i18n_extract", "i18n_check_parity",
        "finalize_patch",
    ):
        assert tname in lead


# ============================================================== MIGRATE

def test_codemod_python_renames_function(py_repo):
    tb = make_toolbox(repo_path=py_repo, repo_full_name="o/r")
    codemod = next(t for t in tb.migrate if t.name == "codemod_python")
    script = """
def transform(source, path):
    return source.replace("def f(", "def g(")
"""
    out = codemod.invoke({"script": script, "glob": "src/*.py"})
    assert "changed: 2" in out
    assert (py_repo / "src" / "a.py").read_text().startswith("def g(")
    assert (py_repo / "src" / "b.py").read_text().startswith("def g(")


def test_codemod_python_rejects_missing_transform(py_repo):
    tb = make_toolbox(repo_path=py_repo, repo_full_name="o/r")
    codemod = next(t for t in tb.migrate if t.name == "codemod_python")
    out = codemod.invoke({"script": "x = 1", "glob": "**/*.py"})
    assert "must define" in out


def test_codemod_python_reports_errors(py_repo):
    tb = make_toolbox(repo_path=py_repo, repo_full_name="o/r")
    codemod = next(t for t in tb.migrate if t.name == "codemod_python")
    script = """
def transform(source, path):
    if "a.py" in path:
        raise ValueError("boom")
    return source
"""
    out = codemod.invoke({"script": script, "glob": "src/*.py"})
    assert "errors: 1" in out
    assert "boom" in out


# ============================================================== PERF

def test_benchmark_run_reports_stats(py_repo):
    tb = make_toolbox(repo_path=py_repo, repo_full_name="o/r")
    bench = next(t for t in tb.perf if t.name == "benchmark_run")
    out = bench.invoke({"command": "true", "runs": 3})
    assert "min" in out and "median" in out and "max" in out


def test_benchmark_run_caps_runs(py_repo):
    tb = make_toolbox(repo_path=py_repo, repo_full_name="o/r")
    bench = next(t for t in tb.perf if t.name == "benchmark_run")
    out = bench.invoke({"command": "true", "runs": 99})
    assert "(x10)" in out


def test_benchmark_run_surfaces_failure(py_repo):
    tb = make_toolbox(repo_path=py_repo, repo_full_name="o/r")
    bench = next(t for t in tb.perf if t.name == "benchmark_run")
    out = bench.invoke({"command": "false", "runs": 1})
    assert "[exit 1]" in out


# ============================================================== I18N

def test_i18n_list_locales(i18n_repo):
    tb = make_toolbox(repo_path=i18n_repo, repo_full_name="o/r")
    lst = next(t for t in tb.i18n if t.name == "i18n_list_locales")
    out = lst.invoke({})
    assert "locales/en.json" in out
    assert "locales/fr.json" in out


def test_i18n_check_parity_detects_drift(i18n_repo):
    tb = make_toolbox(repo_path=i18n_repo, repo_full_name="o/r")
    check = next(t for t in tb.i18n if t.name == "i18n_check_parity")
    out = check.invoke({"reference": "locales/en.json"})
    assert "DRIFT" in out
    assert "nested.b" in out      # missing in fr
    assert "only_en" in out       # missing in fr
    assert "only_fr" in out       # extra in fr


def test_i18n_check_parity_picks_reference_when_omitted(i18n_repo):
    tb = make_toolbox(repo_path=i18n_repo, repo_full_name="o/r")
    check = next(t for t in tb.i18n if t.name == "i18n_check_parity")
    out = check.invoke({})
    # the file with more keys is en.json → that's the reference
    assert "reference: locales/en.json" in out


def test_i18n_extract_handles_missing_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    # No babel.cfg, no xgettext detection, no i18next config → falls through.
    monkeypatch.setattr("shutil.which", lambda name: None)
    tb = make_toolbox(repo_path=tmp_path, repo_full_name="o/r")
    extract = next(t for t in tb.i18n if t.name == "i18n_extract")
    out = extract.invoke({})
    assert "no i18n extractor detected" in out


# ============================================================== AST-GREP (graceful)

def test_ast_grep_missing_binary(py_repo, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None if name == "ast-grep" else "/bin/true")
    tb = make_toolbox(repo_path=py_repo, repo_full_name="o/r")
    search = next(t for t in tb.migrate if t.name == "ast_grep_search")
    out = search.invoke({"pattern": "foo($X)", "lang": "python"})
    assert "ast-grep not installed" in out

    rewrite = next(t for t in tb.migrate if t.name == "ast_grep_rewrite")
    out = rewrite.invoke({"pattern": "foo($X)", "rewrite": "bar($X)", "lang": "python"})
    assert "ast-grep not installed" in out
