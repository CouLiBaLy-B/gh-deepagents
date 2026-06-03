"""Main agent factory: assembles a Deep Agent over a cloned repo.

The agent uses a **specialised sub-agent team** with least-privilege toolsets:

| Sub-agent       | Tools                                       | Can mutate? |
|-----------------|---------------------------------------------|-------------|
| (lead)          | all                                         | yes (PR)    |
| planner         | read-only                                   | no          |
| coder           | read + edit (lint/format)                   | files only  |
| debugger        | read + edit + tests                         | files only  |
| tester          | read + edit + tests                         | files only  |
| reviewer        | read-only                                   | no          |
| security        | read + audit/scan                           | no          |
| deps-manager    | read + edit + audit + tests                 | files only  |
| docs-writer     | read + edit                                 | files only  |
| migrator        | read + edit + ast-grep + codemod            | files only  |
| perf-analyst    | read + run_tests + profilers + benchmarks   | no          |
| i18n            | read + edit + i18n extractors/parity        | files only  |
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from deepagents import create_deep_agent

from .backends import BackendHandle, get_backend_handle
from .github_client import IssueRef
from .models import build_model
from .prompts import (
    CODER_PROMPT,
    DEBUGGER_PROMPT,
    DEPS_MANAGER_PROMPT,
    DOCS_WRITER_PROMPT,
    I18N_PROMPT,
    MAIN_PROMPT,
    MIGRATOR_PROMPT,
    PERF_ANALYST_PROMPT,
    PLANNER_PROMPT,
    REVIEWER_PROMPT,
    SECURITY_PROMPT,
    TESTER_PROMPT,
)
from .tools import make_toolbox


def _instrument_tools(tools: list) -> list:
    """Wrap every tool's `func` with Prometheus counters, duration histograms,
    AND OpenTelemetry spans (no-op when OTel is disabled).
    """
    from .observability.metrics import TOOL_CALLS, TOOL_DURATION
    from .observability.tracing import span
    import functools, time as _time

    out = []
    for t in tools:
        if getattr(t, "_gh_instrumented", False):
            out.append(t); continue
        orig = t.func if hasattr(t, "func") else None
        if orig is None:
            out.append(t); continue
        name = t.name

        @functools.wraps(orig)
        def wrapper(*a, _orig=orig, _name=name, **kw):
            t0 = _time.perf_counter()
            with span(f"tool.{_name}", **{"tool.name": _name}):
                try:
                    r = _orig(*a, **kw)
                    TOOL_CALLS.labels(_name, "ok").inc()
                    return r
                except Exception:
                    TOOL_CALLS.labels(_name, "error").inc()
                    raise
                finally:
                    TOOL_DURATION.labels(_name).observe(_time.perf_counter() - t0)

        t.func = wrapper
        t._gh_instrumented = True   # type: ignore[attr-defined]
        out.append(t)
    return out


def build_agent(
    repo_path: Path,
    repo_full_name: str,
    issue_ref: Optional[IssueRef] = None,
    backend_kind: Optional[str] = None,
    base_branch: Optional[str] = None,
    existing_branch: Optional[str] = None,
) -> tuple[object, BackendHandle]:
    """Return (compiled Deep Agent, backend handle).

    The handle MUST be cleaned up by the caller (`handle.cleanup()`) and is also
    responsible for `sync_to_host()` before committing when running in a remote
    sandbox.
    """
    model = build_model()
    handle = get_backend_handle(repo_path, kind=backend_kind)
    toolbox = make_toolbox(
        repo_path=repo_path,
        repo_full_name=repo_full_name,
        issue_ref=issue_ref,
        backend_handle=handle,
        base_branch=base_branch,
        existing_branch=existing_branch,
    )
    # Wrap every tool list with Prometheus instrumentation (idempotent).
    for attr in ("read_only", "edit", "migrate", "perf", "i18n", "finalize"):
        setattr(toolbox, attr, _instrument_tools(getattr(toolbox, attr)))

    subagents = [
        {
            "name": "planner",
            "description": (
                "Decomposes a vague task into a verifiable plan. READ-ONLY. "
                "Delegate first for any task touching >2 files or >100 LOC."
            ),
            "system_prompt": PLANNER_PROMPT,
            "tools": toolbox.for_role("planner"),
        },
        {
            "name": "coder",
            "description": (
                "Writes/edits source code for a focused spec. Runs lint+format. "
                "No tests, no commits. Delegate for any concrete code change."
            ),
            "system_prompt": CODER_PROMPT,
            "tools": toolbox.for_role("coder"),
        },
        {
            "name": "debugger",
            "description": (
                "Diagnoses a bug or failing test (hypothesis-driven). May edit "
                "to validate. Delegate when a test fails and the cause is unclear."
            ),
            "system_prompt": DEBUGGER_PROMPT,
            "tools": toolbox.for_role("debugger"),
        },
        {
            "name": "tester",
            "description": (
                "Runs the test suite, adds missing tests for a recent change. "
                "Delegate after every coder pass."
            ),
            "system_prompt": TESTER_PROMPT,
            "tools": toolbox.for_role("tester"),
        },
        {
            "name": "reviewer",
            "description": (
                "Critical code review of the current diff. READ-ONLY. Delegate "
                "right before finalize_patch."
            ),
            "system_prompt": REVIEWER_PROMPT,
            "tools": toolbox.for_role("reviewer"),
        },
        {
            "name": "security",
            "description": (
                "Secrets scan + dependency CVE audit + dangerous-pattern search. "
                "Delegate before every finalize_patch."
            ),
            "system_prompt": SECURITY_PROMPT,
            "tools": toolbox.for_role("security"),
        },
        {
            "name": "deps-manager",
            "description": (
                "Adds/removes/bumps dependencies, regenerates lockfiles, audits "
                "CVEs, re-runs tests. Delegate for any dep change."
            ),
            "system_prompt": DEPS_MANAGER_PROMPT,
            "tools": toolbox.for_role("deps-manager"),
        },
        {
            "name": "docs-writer",
            "description": (
                "Updates docstrings, README, CHANGELOG, examples to reflect a "
                "behaviour/API change. Delegate after coder for any public change."
            ),
            "system_prompt": DOCS_WRITER_PROMPT,
            "tools": toolbox.for_role("docs-writer"),
        },
        {
            "name": "migrator",
            "description": (
                "Performs structural rewrites across many files (renames, API "
                "swaps, deprecation removals) using ast-grep / libcst codemods. "
                "Delegate when a change spans >5 files mechanically."
            ),
            "system_prompt": MIGRATOR_PROMPT,
            "tools": toolbox.for_role("migrator"),
        },
        {
            "name": "perf-analyst",
            "description": (
                "Empirical performance work: reproduce, baseline, profile "
                "(py-spy/cProfile), identify hotspot, validate fix with "
                "before/after benchmarks. READ-ONLY w.r.t. files. Delegate "
                "for any 'X is slow' issue."
            ),
            "system_prompt": PERF_ANALYST_PROMPT,
            "tools": toolbox.for_role("perf-analyst"),
        },
        {
            "name": "i18n",
            "description": (
                "Manages translation catalogues: extract new strings, check "
                "parity across locales, add placeholders for new keys. Never "
                "auto-translates. Delegate for any user-facing string change."
            ),
            "system_prompt": I18N_PROMPT,
            "tools": toolbox.for_role("i18n"),
        },
    ]

    agent = create_deep_agent(
        model=model,
        tools=toolbox.for_role("lead"),
        system_prompt=MAIN_PROMPT,
        backend=handle.backend,
        subagents=subagents,
    )
    return agent, handle
