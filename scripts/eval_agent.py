#!/usr/bin/env python3
"""Minimal agent-evaluation harness (P2 scaffold).

Runs the agent in *dry-run* mode over a set of reference tasks, applies the
produced diff to a throwaway clone, and scores each case on objective signals:

  - produced_diff : the agent changed something
  - applies       : the diff applies cleanly
  - tests_pass    : the repo's test suite is green after applying

This is a STARTING POINT, not a full SWE-bench. Extend `CASES` with your own
fixtures (small repos + a clear instruction + an expected outcome) and wire it
into a nightly CI job. Requires a real LLM (DEEPAGENT_MODEL + provider key) and,
for remote repos, GITHUB_TOKEN.

Usage:
    python scripts/eval_agent.py                 # run all cases
    python scripts/eval_agent.py --json          # machine-readable output
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class EvalCase:
    name: str
    repo: str                       # owner/name (cloned) — keep these tiny
    instruction: str
    test_cmd: str = "pytest -q"
    weight: float = 1.0


# Populate with your own small, fast, deterministic fixtures.
CASES: list[EvalCase] = []


def _score_case(case: EvalCase) -> dict:
    from gh_deepagent.runner import evolve_code

    res = evolve_code(case.repo, case.instruction, dry_run=True)
    diff = res.diff or ""
    score = {"name": case.name, "produced_diff": bool(diff.strip()),
             "applies": False, "tests_pass": False}
    if not diff.strip():
        return score

    # Apply the diff to a fresh clone and run the suite.
    with tempfile.TemporaryDirectory() as td:
        from gh_deepagent.github_client import GitHubOps
        repo_path = GitHubOps().clone(case.repo, Path(td))
        patch = Path(td) / "agent.patch"
        patch.write_text(diff, encoding="utf-8")
        applied = subprocess.run(["git", "apply", str(patch)], cwd=repo_path).returncode == 0
        score["applies"] = applied
        if applied:
            rc = subprocess.run(case.test_cmd, shell=True, cwd=repo_path).returncode
            score["tests_pass"] = rc == 0
    return score


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not CASES:
        print("No eval cases configured. Add fixtures to CASES in this file.")
        return 0

    results = [_score_case(c) for c in CASES]
    passed = sum(1 for r in results if r["tests_pass"])
    if args.json:
        print(json.dumps({"results": results, "passed": passed, "total": len(results)}, indent=2))
    else:
        for r in results:
            flags = "".join("✓" if r[k] else "·" for k in ("produced_diff", "applies", "tests_pass"))
            print(f"  [{flags}] {r['name']}")
        print(f"\n{passed}/{len(results)} cases fully passed (tests green).")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
