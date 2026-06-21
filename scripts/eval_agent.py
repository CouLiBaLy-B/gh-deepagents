#!/usr/bin/env python3
"""Agent-evaluation harness over hermetic local fixtures.

Each case is a tiny self-contained repo (a dict of files) plus an instruction
and a *reference solution*. Two modes:

  --check-fixtures   No LLM. Validates every fixture is a sound eval target:
                     the starting repo behaves as declared (e.g. tests fail for
                     a bugfix case), and applying the reference solution makes
                     the checks pass. Run this in CI to keep fixtures honest.

  (default)          Runs the real agent against each fixture in place, then
                     scores objective signals:
                       changed     – the agent modified the repo
                       tests_pass  – the test command is green afterwards
                       checks_ok   – must_contain present / must_not_contain absent
                     Requires a real LLM (DEEPAGENT_MODEL + provider key).

Usage:
    python scripts/eval_agent.py --check-fixtures      # hermetic, no LLM
    python scripts/eval_agent.py                       # run the agent
    python scripts/eval_agent.py --json                # machine-readable
    python scripts/eval_agent.py --only fix-failing-test
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class EvalCase:
    name: str
    instruction: str
    files: dict[str, str]                       # starting repo: path -> content
    solution: dict[str, str]                    # reference fix: path -> content
    test_cmd: str = "python -m pytest -q"
    expect_initial_fail: bool = True            # do the starting tests fail?
    must_contain: list[str] = field(default_factory=list)
    must_not_contain: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
#                                 FIXTURES                                     #
# --------------------------------------------------------------------------- #
# Keep these tiny, fast and deterministic. Each exercises a different agent
# capability (bugfix / edge-case / feature+test / cross-file refactor).

CASES: list[EvalCase] = [
    EvalCase(
        name="fix-failing-test",
        instruction=(
            "The test in test_calc.py is failing. Find and fix the bug in "
            "calc.py so the test passes. Do not change the test."
        ),
        files={
            "calc.py": "def add(a, b):\n    return a - b\n",
            "test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        },
        solution={
            "calc.py": "def add(a, b):\n    return a + b\n",
            "test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        },
        must_contain=["return a + b"],
        must_not_contain=["return a - b"],
    ),
    EvalCase(
        name="fix-zero-division-edge-case",
        instruction=(
            "test_safe_div.py is failing because safe_div crashes on a zero "
            "divisor. Make safe_div(a, 0) return None instead of raising, "
            "without breaking the normal-division test."
        ),
        files={
            "safe_div.py": "def safe_div(a, b):\n    return a / b\n",
            "test_safe_div.py": (
                "from safe_div import safe_div\n\n\n"
                "def test_normal():\n    assert safe_div(6, 2) == 3\n\n\n"
                "def test_zero():\n    assert safe_div(1, 0) is None\n"
            ),
        },
        solution={
            "safe_div.py": (
                "def safe_div(a, b):\n    if b == 0:\n        return None\n    return a / b\n"
            ),
            "test_safe_div.py": (
                "from safe_div import safe_div\n\n\n"
                "def test_normal():\n    assert safe_div(6, 2) == 3\n\n\n"
                "def test_zero():\n    assert safe_div(1, 0) is None\n"
            ),
        },
        must_contain=["b == 0", "return None"],
    ),
    EvalCase(
        name="add-feature-and-test",
        instruction=(
            "Add a function `whisper(s)` to strutils.py that returns s.lower(), "
            "mirroring the existing `shout`. Add a test for it in "
            "test_strutils.py. All tests must pass."
        ),
        files={
            "strutils.py": "def shout(s):\n    return s.upper()\n",
            "test_strutils.py": (
                "from strutils import shout\n\n\n"
                "def test_shout():\n    assert shout('hi') == 'HI'\n"
            ),
        },
        solution={
            "strutils.py": "def shout(s):\n    return s.upper()\n\n\ndef whisper(s):\n    return s.lower()\n",
            "test_strutils.py": (
                "from strutils import shout, whisper\n\n\n"
                "def test_shout():\n    assert shout('hi') == 'HI'\n\n\n"
                "def test_whisper():\n    assert whisper('HI') == 'hi'\n"
            ),
        },
        expect_initial_fail=False,  # the existing test passes before the change
        must_contain=["def whisper", "def test_whisper"],
    ),
    EvalCase(
        name="rename-across-files",
        instruction=(
            "Rename the function `old_name` to `compute` everywhere: its "
            "definition in lib.py and every call site (app.py). Keep the tests "
            "green. Do not leave any reference to `old_name`."
        ),
        files={
            "lib.py": "def old_name():\n    return 1\n",
            "app.py": "from lib import old_name\n\n\ndef run():\n    return old_name()\n",
            "test_app.py": "from app import run\n\n\ndef test_run():\n    assert run() == 1\n",
        },
        solution={
            "lib.py": "def compute():\n    return 1\n",
            "app.py": "from lib import compute\n\n\ndef run():\n    return compute()\n",
            "test_app.py": "from app import run\n\n\ndef test_run():\n    assert run() == 1\n",
        },
        expect_initial_fail=False,  # tests pass before; the win is the clean rename
        must_contain=["def compute"],
        must_not_contain=["old_name"],
    ),
]


# --------------------------------------------------------------------------- #
#                                 HELPERS                                      #
# --------------------------------------------------------------------------- #

def _write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _init_repo(root: Path, files: dict[str, str]) -> str:
    _write_files(root, files)
    env = ["-c", "user.email=eval@local", "-c", "user.name=eval"]
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", *env, "commit", "-q", "-m", "init"], cwd=root, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()


def _tests_pass(root: Path, cmd: str) -> bool:
    return subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True).returncode == 0


def _changed_since(root: Path, sha: str) -> bool:
    diff = subprocess.run(["git", "diff", sha], cwd=root, capture_output=True, text=True).stdout
    return bool(diff.strip())


def _checks_ok(root: Path, case: EvalCase) -> bool:
    blob = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.parts
    )
    if any(s not in blob for s in case.must_contain):
        return False
    if any(s in blob for s in case.must_not_contain):
        return False
    return True


def _run_agent_local(root: Path, instruction: str) -> None:
    """Drive the real agent against a local checkout (no GitHub, no PR)."""
    from gh_deepagent.agent import build_agent
    from gh_deepagent.config import get_settings
    from gh_deepagent.runner import _stream

    agent, handle = build_agent(repo_path=root, repo_full_name="eval/local")
    try:
        prompt = (
            f"Working directory is a local repo checkout.\n\n"
            f"Task:\n{instruction}\n\n"
            f"Make the edits and run the tests until green. "
            f"DO NOT call finalize_patch and DO NOT open a pull request — just "
            f"leave the changes in the working tree and stop."
        )
        _stream(agent, {"messages": [{"role": "user", "content": prompt}]},
                get_settings().max_turns)
    finally:
        handle.cleanup()


# --------------------------------------------------------------------------- #
#                                  MODES                                       #
# --------------------------------------------------------------------------- #

def check_fixtures(cases: list[EvalCase]) -> list[dict]:
    """No LLM. Prove each fixture is a sound eval target."""
    results = []
    for c in cases:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root, c.files)
            before = _tests_pass(root, c.test_cmd)
            initial_ok = (before is False) if c.expect_initial_fail else (before is True)
            # Apply the reference solution and re-check.
            _write_files(root, c.solution)
            after = _tests_pass(root, c.test_cmd)
            checks = _checks_ok(root, c)
        results.append({
            "name": c.name,
            "initial_state_ok": initial_ok,
            "solution_tests_pass": after,
            "solution_checks_ok": checks,
            "sound": initial_ok and after and checks,
        })
    return results


def run_agent(cases: list[EvalCase]) -> list[dict]:
    """Run the real agent against each fixture and score it."""
    results = []
    for c in cases:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sha = _init_repo(root, c.files)
            try:
                _run_agent_local(root, c.instruction)
                err = None
            except Exception as e:  # keep going across cases
                err = f"{type(e).__name__}: {e}"
            changed = _changed_since(root, sha)
            tests = _tests_pass(root, c.test_cmd)
            checks = _checks_ok(root, c)
        results.append({
            "name": c.name, "changed": changed, "tests_pass": tests,
            "checks_ok": checks, "solved": tests and checks, "error": err,
        })
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-fixtures", action="store_true",
                    help="Validate fixtures without an LLM.")
    ap.add_argument("--only", help="Run only the case with this name.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cases = [c for c in CASES if not args.only or c.name == args.only]
    if not cases:
        print(f"No case named {args.only!r}. Available: {[c.name for c in CASES]}")
        return 2

    if args.check_fixtures:
        results = check_fixtures(cases)
        ok = sum(1 for r in results if r["sound"])
        if args.json:
            print(json.dumps({"results": results, "sound": ok, "total": len(results)}, indent=2))
        else:
            for r in results:
                mark = "OK " if r["sound"] else "BAD"
                print(f"  [{mark}] {r['name']}  "
                      f"(initial={'ok' if r['initial_state_ok'] else 'WRONG'}, "
                      f"solved_by_solution={'yes' if r['solution_tests_pass'] else 'NO'}, "
                      f"checks={'yes' if r['solution_checks_ok'] else 'NO'})")
            print(f"\n{ok}/{len(results)} fixtures are sound.")
        return 0 if ok == len(results) else 1

    results = run_agent(cases)
    solved = sum(1 for r in results if r["solved"])
    if args.json:
        print(json.dumps({"results": results, "solved": solved, "total": len(results)}, indent=2))
    else:
        for r in results:
            flags = "".join("✓" if r[k] else "·" for k in ("changed", "tests_pass", "checks_ok"))
            extra = f"  ! {r['error']}" if r["error"] else ""
            print(f"  [{flags}] {r['name']}{extra}")
        print(f"\n{solved}/{len(results)} cases solved (tests green + checks).")
    return 0 if solved == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
