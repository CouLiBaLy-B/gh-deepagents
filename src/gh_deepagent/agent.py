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

import inspect
from pathlib import Path
from typing import Optional

from deepagents import create_deep_agent

from .backends import BackendHandle, get_backend_handle
from .config import get_settings
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


# Sub-agents that do read-only or light-touch work → run on the cheap model
# when DEEPAGENT_MODEL_CHEAP is configured. coder/debugger/tester/deps-manager/
# migrator/perf-analyst stay on the strong model (the critical correctness path).
CHEAP_ROLES = {"planner", "reviewer", "security", "docs-writer", "i18n"}

# Tools (destructive / expensive) that trigger human approval in --interactive.
INTERRUPT_TOOLS = ("finalize_patch", "codemod_python", "ast_grep_rewrite")

# Bundled skills library shipped with gh-deepagent (SKILL.md per sub-directory).
# Loaded through the backend at an *absolute host path*; only works for the
# local backend (a remote sandbox doesn't have this path) — skipped otherwise.
SKILLS_DIR = Path(__file__).resolve().parents[2] / ".deepagents" / "skills"


def _supports(func, name: str) -> bool:
    """True if `func` accepts a keyword argument `name` (forward/back-compat)."""
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins without sig
        return False





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
    interactive: Optional[bool] = None,
) -> tuple[object, BackendHandle]:
    """Return (compiled Deep Agent, backend handle).

    The handle MUST be cleaned up by the caller (`handle.cleanup()`) and is also
    responsible for `sync_to_host()` before committing when running in a remote
    sandbox.

    `interactive` enables human-in-the-loop approval for destructive tools; when
    None it falls back to the `DEEPAGENT_INTERACTIVE` setting.
    """
    settings = get_settings()
    model = build_model()
    # Cheap model for read-only / light sub-agents (cost lever). Falls back to
    # the main model when DEEPAGENT_MODEL_CHEAP is unset.
    cheap_model = build_model(settings.model_cheap) if settings.model_cheap else model
    if interactive is None:
        interactive = settings.interactive
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

    # --- Skills library (local backend only) -------------------------
    # Skills are read through the backend; for the local backend we hand it the
    # absolute host path of the bundled library. Remote sandboxes don't have
    # this path, so we skip skills there (graceful — the prompts still work).
    skills_sources: list[str] = []
    if not handle.is_remote and SKILLS_DIR.is_dir():
        skills_sources = [str(SKILLS_DIR)]

    # --- Per-sub-agent model + skills --------------------------------
    for sa in subagents:
        if sa["name"] in CHEAP_ROLES and cheap_model is not model:
            sa["model"] = cheap_model
        # Custom sub-agents do NOT inherit the lead's skills automatically.
        if skills_sources:
            sa["skills"] = skills_sources

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

    # --- Native skills + memory + HITL (gated by deepagents support) --
    # We only forward kwargs the installed deepagents actually accepts, so the
    # wide version pin (>=0.6.11,<0.8) keeps working even as the API evolves.
    if skills_sources and _supports(create_deep_agent, "skills"):
        extra_kwargs["skills"] = skills_sources

    # Native memory: load the target repo's AGENTS.md (deepagents tolerates it
    # being absent) so repo-local conventions are always in context. This
    # complements — does not replace — the layered /memories/<repo>/ store.
    if not handle.is_remote and _supports(create_deep_agent, "memory"):
        extra_kwargs["memory"] = ["/AGENTS.md"]

    # Human-in-the-loop approval for destructive tools (opt-in).
    if interactive and _supports(create_deep_agent, "interrupt_on"):
        extra_kwargs["interrupt_on"] = {t: True for t in INTERRUPT_TOOLS}
        try:
            from langgraph.checkpoint.memory import MemorySaver
            extra_kwargs.setdefault("checkpointer", MemorySaver())
        except Exception:  # pragma: no cover - langgraph always present here
            pass

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
