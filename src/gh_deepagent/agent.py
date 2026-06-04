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





def _review_response_format():
    """Return the Pydantic schema for the reviewer's structured output.

    Wrapped in a function so importing this module never fails if the
    review_schema dependencies (Pydantic) aren't available at import time.
    """
    try:
        from .review_schema import ReviewReport
        return ReviewReport
    except Exception:
        return None


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
    handle = get_backend_handle(repo_path, kind=backend_kind, repo_full_name=repo_full_name)
    toolbox = make_toolbox(
        repo_path=repo_path,
        repo_full_name=repo_full_name,
        issue_ref=issue_ref,
        backend_handle=handle,
        base_branch=base_branch,
        existing_branch=existing_branch,
    )
    # Tool/model instrumentation now lives in MetricsMiddleware (wired below)
    # — no need to monkey-patch each tool function anymore.

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
                "right before finalize_patch. Returns a structured ReviewReport."
            ),
            "system_prompt": REVIEWER_PROMPT,
            "tools": toolbox.for_role("reviewer"),
            # Structured output via Pydantic — falls back to free-form text
            # if the installed deepagents version doesn't honour the field.
            "response_format": _review_response_format(),
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

    # --- Layered-memory wiring ---------------------------------------
    # When the backend is layered (handle.memory_path set), provide the
    # StoreBackend with an InMemoryStore and tell the agent where to look.
    extra_kwargs: dict = {}
    system_prompt = MAIN_PROMPT
    if handle.memory_path:
        try:
            from langgraph.store.memory import InMemoryStore
            extra_kwargs["store"] = InMemoryStore()
        except Exception:
            pass
        system_prompt = (
            MAIN_PROMPT
            + f"\n\n## Persistent memory\n\n"
            + f"You have a long-term memory area at `{handle.memory_path}`. "
            + f"It persists across jobs on this repo (conventions, past "
            + f"decisions, 'do not touch' notes). Read it at the start of "
            + f"every job (`ls {handle.memory_path}` then `read_file`) and "
            + f"WRITE durable observations there with `write_file` (NEVER use "
            + f"it as scratch space — use the working dir for that)."
        )

    # MetricsMiddleware is appended to the default deep-agent stack and runs
    # on every tool + model call (sync and async). See observability/middleware.py.
    from .observability.middleware import MetricsMiddleware
    user_middleware = [MetricsMiddleware()]

    agent = create_deep_agent(
        model=model,
        tools=toolbox.for_role("lead"),
        system_prompt=system_prompt,
        backend=handle.backend,
        subagents=subagents,
        middleware=user_middleware,
        **extra_kwargs,
    )
    return agent, handle
