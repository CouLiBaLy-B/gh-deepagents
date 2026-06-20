"""Custom tools exposed to the deep agent.

Tools are organised in **privilege tiers** so we can hand the right subset to
the right sub-agent (least-privilege principle):

- `read_only_tools`   — never mutate the repo or GitHub
- `edit_tools`        — can run shell commands that mutate files locally (lint --fix, format)
- `migrate_tools`     — codemod / structural rewrite (ast-grep). Powerful but scoped.
- `perf_tools`        — read-only profiling (py-spy, cProfile, benchmark runs)
- `i18n_tools`        — extract/sync translation strings, check parity
- `finalize_tools`    — can commit/push and open/update PRs (LEAD AGENT ONLY)

Heavy file I/O is still handled by the Deep Agents backend (LocalShellBackend or
remote sandbox). We add only the GitHub-specific verbs + safety nets + ergonomic
typed wrappers around shell commands.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from langchain_core.tools import tool

from .github_client import GitHubOps, IssueRef

if TYPE_CHECKING:
    from .backends import BackendHandle


# =================================================================
#                          TOOLBOX FACTORY
# =================================================================

@dataclass
class Toolbox:
    """Bundle of tools split by privilege. Pass the right subset to each sub-agent."""
    read_only: list
    edit: list
    migrate: list
    perf: list
    i18n: list
    finalize: list

    @property
    def all(self) -> list:
        return self.read_only + self.edit + self.migrate + self.perf + self.i18n + self.finalize

    def for_role(self, role: str) -> list:
        """Return the right subset for a sub-agent role."""
        if role == "lead":
            return self.all
        if role in {"planner", "reviewer", "security", "docs-reader"}:
            return self.read_only
        if role in {"coder", "tester", "debugger", "deps-manager", "docs-writer"}:
            return self.read_only + self.edit
        if role == "migrator":
            # read + edit (to apply formatters after a rewrite) + migrate tools
            return self.read_only + self.edit + self.migrate
        if role == "perf-analyst":
            # read-only profiling + ability to run tests/benchmarks
            return self.read_only + self.perf + [t for t in self.edit if t.name == "run_tests"]
        if role == "i18n":
            return self.read_only + self.edit + self.i18n
        raise ValueError(f"Unknown role: {role}")


def make_toolbox(
    repo_path: Path,
    repo_full_name: str,
    issue_ref: Optional[IssueRef] = None,
    backend_handle: Optional["BackendHandle"] = None,
    base_branch: Optional[str] = None,
    existing_branch: Optional[str] = None,
) -> Toolbox:
    """Build the full tool registry, segmented by privilege."""
    gh = GitHubOps()

    # ---------------------------------------------------------- READ ONLY
    @tool
    def fetch_issue(issue_number: int) -> str:
        """Fetch a GitHub issue (title, body, labels, comments) as JSON.

        Use when you need full context for an issue you don't already have.
        """
        ref = IssueRef(*repo_full_name.split("/"), number=issue_number)
        return json.dumps(gh.fetch_issue_context(ref), indent=2, default=str)

    @tool
    def list_project_files(glob: str = "**/*") -> str:
        """List repo files matching a glob (default: all). For orientation. Returns up to 500 paths."""
        paths = sorted(str(p.relative_to(repo_path)) for p in repo_path.glob(glob) if p.is_file())
        return "\n".join(paths[:500])

    @tool
    def search_code(pattern: str, glob: str = "", max_results: int = 80) -> str:
        """Ripgrep-style search across the repo. Returns `path:line: match` lines.

        Args:
            pattern: regex (PCRE2 if rg, else POSIX). Quote special chars.
            glob: optional file glob, e.g. "*.py" or "src/**/*.ts".
            max_results: cap on returned lines.
        """
        rg = shutil.which("rg")
        if rg:
            cmd = [rg, "-n", "--no-heading", "--color", "never", "-S", pattern]
            if glob:
                cmd += ["-g", glob]
        else:
            cmd = ["grep", "-rnIE", "--color=never", pattern, "."]
            if glob:
                cmd = ["bash", "-lc", f"grep -rnIE --include='{glob}' --color=never {pattern!r} ."]
        try:
            out = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, timeout=60).stdout
        except Exception as e:  # pragma: no cover
            return f"search failed: {e}"
        lines = out.splitlines()[:max_results]
        return "\n".join(lines) or "(no matches)"

    @tool
    def read_file_range(path: str, start: int = 1, end: int = 200) -> str:
        """Read a 1-indexed line range from a file (capped at 800 lines)."""
        end = min(end, start + 800)
        p = repo_path / path
        if not p.is_file():
            return f"not a file: {path}"
        lines = p.read_text(errors="replace").splitlines()
        slice_ = lines[start - 1 : end]
        return "\n".join(f"{i + start}: {ln}" for i, ln in enumerate(slice_))

    @tool
    def git_log(path: str = "", n: int = 10) -> str:
        """Show the last `n` commits (oneline). Optionally scoped to a path."""
        cmd = ["git", "log", "--oneline", f"-n{n}"]
        if path:
            cmd += ["--", path]
        return subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True).stdout or "(no commits)"

    @tool
    def git_blame(path: str, line: int) -> str:
        """Show `git blame -L line,line path` for line-level archaeology."""
        out = subprocess.run(
            ["git", "blame", "-L", f"{line},{line}", path],
            cwd=repo_path, capture_output=True, text=True,
        )
        return out.stdout or out.stderr or "(no output)"

    @tool
    def summarize_diff(against: str = "HEAD") -> str:
        """Return the current diff vs `against` (default HEAD, truncated to 12k chars)."""
        if backend_handle and backend_handle.is_remote:
            backend_handle.sync_to_host()
        try:
            diff = subprocess.check_output(
                ["git", "diff", against], cwd=repo_path, text=True, stderr=subprocess.DEVNULL
            )
        except subprocess.CalledProcessError as e:
            return f"git diff failed: {e}"
        return diff[:12000] or "(no changes)"

    @tool
    def analyze_test_failure(test_output: str) -> str:
        """Extract a structured summary from raw pytest/jest/go test output.

        Returns JSON: {"runner": ..., "failed": [{"name","file","line","error","trace"}], "passed": N, "failed_count": N}
        """
        text = test_output or ""
        result = {"runner": _detect_runner(text), "failed": [], "passed": 0, "failed_count": 0}

        # pytest
        for m in re.finditer(
            r"_+ (?P<name>\S+) _+\n(?P<body>.*?)(?=\n_+ \S+ _+\n|\n=+ short test summary|$)",
            text, re.S,
        ):
            body = m.group("body")
            file_line = re.search(r"^(?P<file>[\w./-]+):(?P<line>\d+):", body, re.M)
            err = re.search(r"^E\s+(?P<err>.+)", body, re.M)
            result["failed"].append({
                "name": m.group("name"),
                "file": file_line.group("file") if file_line else None,
                "line": int(file_line.group("line")) if file_line else None,
                "error": err.group("err") if err else None,
                "trace": body[-1200:],
            })
        m = re.search(r"=+ (?P<f>\d+) failed,? ?(?P<p>\d+)? passed", text)
        if m:
            result["failed_count"] = int(m.group("f"))
            result["passed"] = int(m.group("p") or 0)
        return json.dumps(result, indent=2)

    # ---------------------------------------------------------- EDIT
    @tool
    def run_tests(command: str = "") -> str:
        """Run the project's test suite. Returns stdout+stderr (truncated).

        With no arg, auto-detects pytest / npm test / go test / cargo test / make test.
        """
        cmd = command or _autodetect_test_cmd(repo_path)
        if not cmd:
            return "No test runner detected. Skipping."
        if backend_handle and backend_handle.is_remote:
            res = backend_handle.backend.execute(f"cd /workspace/repo && {cmd}")
            out = getattr(res, "output", "") or ""
            return f"$ {cmd}\n[exit {getattr(res, 'exit_code', '?')}]\n{out[-8000:]}"
        proc = subprocess.run(
            cmd, shell=True, cwd=repo_path, capture_output=True, text=True, timeout=900
        )
        return f"$ {cmd}\n[exit {proc.returncode}]\n{(proc.stdout or '') + (proc.stderr or '')}"[-8000:]

    @tool
    def lint_check(fix: bool = False) -> str:
        """Run the project's linter. Auto-detects ruff / eslint / golangci-lint / clippy.

        Set fix=True to apply autofixes.
        """
        cmds = _autodetect_lint_cmd(repo_path, fix=fix)
        if not cmds:
            return "No linter detected."
        outputs = []
        for cmd in cmds:
            p = subprocess.run(cmd, shell=True, cwd=repo_path, capture_output=True, text=True, timeout=300)
            outputs.append(f"$ {cmd}\n[exit {p.returncode}]\n{(p.stdout or '') + (p.stderr or '')}"[-3000:])
        return "\n\n".join(outputs)

    @tool
    def format_code() -> str:
        """Run the project's formatter (ruff format / prettier / gofmt / cargo fmt)."""
        cmd = _autodetect_fmt_cmd(repo_path)
        if not cmd:
            return "No formatter detected."
        p = subprocess.run(cmd, shell=True, cwd=repo_path, capture_output=True, text=True, timeout=300)
        return f"$ {cmd}\n[exit {p.returncode}]\n{(p.stdout or '') + (p.stderr or '')}"[-4000:]

    @tool
    def dependency_audit() -> str:
        """Audit dependencies for known CVEs (pip-audit / npm audit / cargo audit)."""
        cmd = _autodetect_audit_cmd(repo_path)
        if not cmd:
            return "No supported dependency manager found."
        p = subprocess.run(cmd, shell=True, cwd=repo_path, capture_output=True, text=True, timeout=300)
        return f"$ {cmd}\n[exit {p.returncode}]\n{(p.stdout or '') + (p.stderr or '')}"[-6000:]

    @tool
    def scan_secrets() -> str:
        """Run `gitleaks detect` on the working tree. Returns its report (or 'no leaks')."""
        if not shutil.which("gitleaks"):
            return "gitleaks not installed; SKIP (install with `brew install gitleaks` or apt)."
        p = subprocess.run(
            ["gitleaks", "detect", "--no-banner", "--redact", "--source", str(repo_path)],
            capture_output=True, text=True, timeout=120,
        )
        return f"[exit {p.returncode}]\n{(p.stdout or '') + (p.stderr or '')}"[-6000:] or "no leaks"

    # ========================================================== MIGRATE
    @tool
    def ast_grep_search(pattern: str, lang: str = "", path: str = "") -> str:
        """Structural code search via ast-grep. Returns matches with file:line.

        Pattern uses ast-grep's pattern syntax (e.g. `requests.get($URL)` to
        match any call). Falls back to a clear message if ast-grep is missing.

        Args:
            pattern: ast-grep pattern. Use `$VAR` for placeholders, `$$$ARGS` for variadic.
            lang: source language hint (python, javascript, typescript, go, rust, ...).
            path: scope to a subpath. Defaults to whole repo.
        """
        if not shutil.which("ast-grep"):
            return "ast-grep not installed. Install: `cargo install ast-grep` or `npm i -g @ast-grep/cli`."
        cmd = ["ast-grep", "run", "--pattern", pattern]
        if lang:
            cmd += ["--lang", lang]
        if path:
            cmd += [path]
        try:
            out = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, timeout=120)
        except Exception as e:  # pragma: no cover
            return f"ast-grep failed: {e}"
        body = (out.stdout or "") + (out.stderr or "")
        return body[-8000:] or "(no matches)"

    @tool
    def ast_grep_rewrite(
        pattern: str,
        rewrite: str,
        lang: str = "",
        path: str = "",
        apply: bool = False,
    ) -> str:
        """Structural rewrite. By default returns a dry-run diff; set apply=True to write.

        Use for bulk transformations the LLM is *guaranteed* to get right
        (rename function, swap call sites, modernise API). For each change you
        can't express structurally, fall back to per-file edits.

        Args:
            pattern: source pattern. e.g. `foo($X)`.
            rewrite: replacement template referencing the same metavars. e.g. `bar($X, default=None)`.
            lang: source language.
            path: subpath scope.
            apply: write changes when True; otherwise show diff only.
        """
        if not shutil.which("ast-grep"):
            return "ast-grep not installed. Cannot perform structural rewrite."
        cmd = ["ast-grep", "run", "--pattern", pattern, "--rewrite", rewrite]
        if lang:
            cmd += ["--lang", lang]
        if path:
            cmd += [path]
        if apply:
            cmd += ["--update-all"]
        try:
            out = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, timeout=300)
        except Exception as e:  # pragma: no cover
            return f"ast-grep failed: {e}"
        body = (out.stdout or "") + (out.stderr or "")
        verdict = "APPLIED" if apply else "DRY-RUN (rerun with apply=True to write)"
        return f"[{verdict}]\n{body[-8000:]}"

    @tool
    def codemod_python(script: str, glob: str = "**/*.py") -> str:
        """Apply a Python codemod on every file matching `glob`.

        `script` must define a `transform(source: str, path: str) -> str` function.
        Useful when ast-grep patterns aren't expressive enough (e.g. need to
        cross-reference imports, manipulate decorators, infer types).

        The script can import `libcst` (install separately) for concrete-syntax
        manipulation, or use plain string operations / ast / re for simpler cases.

        SECURITY: the (LLM-authored) script is executed in an **isolated child
        process** with a scrubbed environment and a hard timeout — never in the
        long-lived agent process. The migrator sub-agent is the only one allowed
        to use this tool.
        """
        return _run_codemod_subprocess(repo_path, script, glob)

    # ========================================================== PERF
    @tool
    def benchmark_run(command: str, runs: int = 5) -> str:
        """Run a shell command multiple times and report min/median/max wall time.

        Use to validate a perf hypothesis (before/after numbers). Caps at 10 runs.
        """
        import statistics as _stats
        import time as _time

        runs = max(1, min(runs, 10))
        times: list[float] = []
        last_out = ""
        for _ in range(runs):
            t0 = _time.perf_counter()
            p = subprocess.run(
                command, shell=True, cwd=repo_path, capture_output=True, text=True, timeout=900
            )
            times.append(_time.perf_counter() - t0)
            last_out = (p.stdout or "") + (p.stderr or "")
            if p.returncode != 0:
                return f"$ {command}\n[exit {p.returncode}]\n{last_out[-2000:]}"
        return (
            f"$ {command}  (x{runs})\n"
            f"  min    = {min(times):.3f}s\n"
            f"  median = {_stats.median(times):.3f}s\n"
            f"  max    = {max(times):.3f}s\n"
            f"  stdev  = {(_stats.pstdev(times) if len(times) > 1 else 0):.3f}s\n"
            f"---\n{last_out[-1500:]}"
        )

    @tool
    def profile_python(script_or_module: str, duration: int = 10, native: bool = False) -> str:
        """Sample-profile a Python target with py-spy, then return the top hotspots.

        Args:
            script_or_module: command to launch (e.g. `python -m mypkg.main args`).
            duration: how many seconds to sample (capped at 60).
            native: include C-extension frames (numpy, pandas).
        """
        if not shutil.which("py-spy"):
            return "py-spy not installed. `pip install py-spy` to enable Python profiling."
        duration = max(1, min(duration, 60))
        out_path = repo_path / ".gh-deepagent-profile.svg"
        cmd = [
            "py-spy", "record", "-o", str(out_path),
            "--duration", str(duration), "--format", "flamegraph",
        ]
        if native:
            cmd.append("--native")
        cmd += ["--"] + script_or_module.split()
        try:
            proc = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, timeout=duration + 30)
        except subprocess.TimeoutExpired:
            return f"py-spy timed out after {duration + 30}s."
        # Also grab a 1-shot `dump` for a quick textual hotspot list.
        top_text = ""
        try:
            top = subprocess.run(
                ["py-spy", "dump", "--pid", "$(pgrep -nf '" + script_or_module.split()[-1] + "')"],
                shell=False, capture_output=True, text=True, timeout=10,
            )
            top_text = top.stdout
        except Exception:
            pass
        return (
            f"profile written to {out_path} (open in browser)\n"
            f"[exit {proc.returncode}]\n{(proc.stdout or '') + (proc.stderr or '')}\n"
            f"--- live dump ---\n{top_text[:2000]}"
        )

    @tool
    def cprofile_run(command: str, top: int = 25) -> str:
        """Run a Python command under cProfile and return the top-N hottest funcs.

        `command` is run as `python -c "..."` if it starts with `-`, otherwise
        executed verbatim. We capture cProfile stats and dump them as text.
        """
        if not command.strip():
            return "command required."
        wrapper = (
            "import cProfile, pstats, io, runpy, sys; "
            "pr = cProfile.Profile(); pr.enable(); "
            f"exec(compile(open({command.split()[-1]!r}).read(),{command.split()[-1]!r},'exec')) "
            "if False else __import__('subprocess').run("
            f"{command.split()!r}, check=False); "
            "pr.disable(); s=io.StringIO(); "
            f"pstats.Stats(pr, stream=s).sort_stats('cumulative').print_stats({top}); "
            "print(s.getvalue())"
        )
        # Simpler: invoke python -m cProfile if the user passed a python invocation.
        if command.startswith("python "):
            real = ["python", "-m", "cProfile", "-s", "cumulative"] + command.split()[1:]
            p = subprocess.run(real, cwd=repo_path, capture_output=True, text=True, timeout=600)
            return f"$ {' '.join(real)}\n[exit {p.returncode}]\n{(p.stdout or '')[-8000:]}"
        # Otherwise, run as-is and just time it.
        p = subprocess.run(command, shell=True, cwd=repo_path, capture_output=True, text=True, timeout=600)
        return f"$ {command}\n[exit {p.returncode}]\n{(p.stdout or '')[-8000:]}"

    @tool
    def perf_compare(label_a: str, command_a: str, label_b: str, command_b: str, runs: int = 5) -> str:
        """Run two commands `runs` times each and report a side-by-side comparison."""
        a = benchmark_run.invoke({"command": command_a, "runs": runs})
        b = benchmark_run.invoke({"command": command_b, "runs": runs})
        return f"## {label_a}\n{a}\n\n## {label_b}\n{b}\n"

    # ========================================================== I18N
    @tool
    def i18n_list_locales() -> str:
        """Find translation catalogues in the repo and return their paths + key counts.

        Heuristics: `*.po`, `*.pot`, `locales/<lang>/*.json`, `messages/<lang>.json`,
        `i18n/<lang>.yml`, `translations/**`.
        """
        patterns = [
            "**/*.po", "**/*.pot",
            "**/locales/**/*.json", "**/locales/**/*.yml", "**/locales/**/*.yaml",
            "**/messages/**/*.json", "**/i18n/**/*.json", "**/i18n/**/*.yml",
            "**/translations/**/*.json",
        ]
        found: dict[str, int] = {}
        for pat in patterns:
            for p in repo_path.glob(pat):
                if not p.is_file() or ".git" in p.parts:
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                # Crude key count
                if p.suffix in {".po", ".pot"}:
                    keys = text.count("\nmsgid ")
                elif p.suffix == ".json":
                    keys = text.count('":')
                else:
                    keys = text.count(":")
                found[str(p.relative_to(repo_path))] = keys
        if not found:
            return "(no translation catalogues found)"
        return "\n".join(f"{path}\t{keys} keys" for path, keys in sorted(found.items()))

    @tool
    def i18n_extract(catalog: str = "") -> str:
        """Extract translatable strings from the source into the catalog.

        Auto-detects framework:
        - Python: pybabel (django/flask) or xgettext if available
        - JS/TS: i18next-parser, formatjs extract, or grep `t("...")`
        - Go: goi18n
        Returns the command run and its output.
        """
        # Python
        if (repo_path / "babel.cfg").exists() and shutil.which("pybabel"):
            cat = catalog or "messages.pot"
            cmd = f"pybabel extract -F babel.cfg -o {cat} ."
        elif shutil.which("xgettext") and any(repo_path.glob("**/*.py")):
            cat = catalog or "messages.pot"
            files = " ".join(str(p) for p in repo_path.rglob("*.py"))
            cmd = f"xgettext --language=Python --keyword=_ --keyword=gettext --output={cat} {files}"
        # JS/TS via i18next-parser
        elif (repo_path / "i18next-parser.config.js").exists() and shutil.which("npx"):
            cmd = "npx --no-install i18next-parser"
        # Fallback: grep
        else:
            return (
                "no i18n extractor detected.\n"
                "Install one of: pybabel, xgettext, i18next-parser.\n"
                "As a fallback, use `search_code` with patterns like `t\\(\"` or `gettext\\(`."
            )
        p = subprocess.run(cmd, shell=True, cwd=repo_path, capture_output=True, text=True, timeout=300)
        return f"$ {cmd}\n[exit {p.returncode}]\n{((p.stdout or '') + (p.stderr or ''))[-4000:]}"

    @tool
    def i18n_check_parity(reference: str = "") -> str:
        """Check that all locale files have the same keys as the reference locale.

        Args:
            reference: path to the reference locale (e.g. `locales/en.json`).
                       If empty, picks the largest `*.json` under `locales/` or `i18n/`.
        Returns a Markdown report of missing/extra keys per locale.
        """
        import json as _json

        # Find candidate files
        candidates = sorted(
            list(repo_path.glob("**/locales/*.json"))
            + list(repo_path.glob("**/i18n/*.json"))
            + list(repo_path.glob("**/messages/*.json"))
        )
        if not candidates:
            return "(no JSON locale files found)"
        if reference:
            ref_path = repo_path / reference
        else:
            ref_path = max(candidates, key=lambda p: p.stat().st_size)

        def flatten(d, prefix=""):
            out = set()
            for k, v in d.items():
                key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    out |= flatten(v, key)
                else:
                    out.add(key)
            return out

        try:
            ref_keys = flatten(_json.loads(ref_path.read_text(encoding="utf-8")))
        except Exception as e:
            return f"failed to read reference {ref_path}: {e}"
        report = [f"reference: {ref_path.relative_to(repo_path)} ({len(ref_keys)} keys)"]
        for c in candidates:
            if c == ref_path:
                continue
            try:
                keys = flatten(_json.loads(c.read_text(encoding="utf-8")))
            except Exception as e:
                report.append(f"\n### {c.relative_to(repo_path)} — UNREADABLE ({e})")
                continue
            missing = sorted(ref_keys - keys)
            extra = sorted(keys - ref_keys)
            status = "OK" if not missing and not extra else "DRIFT"
            report.append(f"\n### {c.relative_to(repo_path)} — {status}")
            if missing:
                report.append(f"  missing ({len(missing)}): " + ", ".join(missing[:20]))
            if extra:
                report.append(f"  extra   ({len(extra)}): " + ", ".join(extra[:20]))
        return "\n".join(report)

    # ---------------------------------------------------------- FINALIZE
    @tool
    def finalize_patch(branch_name: str, commit_message: str, pr_title: str, pr_body: str) -> str:
        """Commit, push, and open/update a PR. **LEAD AGENT ONLY.** Call once at the end.

        If an existing branch is configured (iterate-on-PR mode), pushes to it
        and posts an update comment instead of creating a new PR.
        """
        if backend_handle and backend_handle.is_remote:
            backend_handle.sync_to_host()

        # Safety net: refuse to push to default-protected branches.
        if branch_name in {"main", "master", base_branch or ""}:
            return f"REFUSED: branch_name {branch_name!r} would target the protected base."

        target_branch = existing_branch or branch_name
        if not _branch_exists(repo_path, target_branch):
            gh.create_branch(repo_path, target_branch)
        else:
            subprocess.run(["git", "checkout", target_branch], cwd=repo_path, check=False)

        committed = gh.commit_all(repo_path, commit_message)
        if not committed:
            return "No changes to commit. Aborting PR update."
        gh.push(repo_path, target_branch, full_name=repo_full_name)

        if existing_branch:
            pr = _find_open_pr_for_branch(gh, repo_full_name, existing_branch)
            if pr:
                pr.create_issue_comment(f"🤖 gh-deepagent pushed an update:\n\n{pr_body}")
                return f"PR updated: {pr.html_url}"

        body = pr_body
        if issue_ref:
            body += f"\n\nCloses #{issue_ref.number}"
        pr = gh.open_pr(repo_full_name, target_branch, pr_title, body, base=base_branch or "main")
        return f"PR opened: {pr.html_url}"

    # ---------------------------------------------------------- BUNDLE
    return Toolbox(
        read_only=[
            fetch_issue, list_project_files, search_code, read_file_range,
            git_log, git_blame, summarize_diff, analyze_test_failure,
        ],
        edit=[run_tests, lint_check, format_code, dependency_audit, scan_secrets],
        migrate=[ast_grep_search, ast_grep_rewrite, codemod_python],
        perf=[benchmark_run, profile_python, cprofile_run, perf_compare],
        i18n=[i18n_list_locales, i18n_extract, i18n_check_parity],
        finalize=[finalize_patch],
    )


# Backwards-compat alias used by old callers.
def make_tools(*args, **kwargs):  # pragma: no cover - thin shim
    return make_toolbox(*args, **kwargs).all


# =================================================================
#                       CODEMOD (ISOLATED)
# =================================================================

# Harness executed in a child process. It imports the user-supplied `transform`,
# applies it across the glob, writes changes, and prints a report in the SAME
# format the in-process implementation used to (so behaviour is unchanged).
_CODEMOD_HARNESS = r'''
import sys
from pathlib import Path

script_path, repo_str, glob = sys.argv[1], sys.argv[2], sys.argv[3]
repo_path = Path(repo_str)
source_code = Path(script_path).read_text(encoding="utf-8")

ns = {}
try:
    exec(compile(source_code, "<codemod>", "exec"), ns)
except Exception as e:
    print(f"codemod script failed to compile: {e}")
    sys.exit(0)

transform = ns.get("transform")
if not callable(transform):
    print("codemod script must define `transform(source, path) -> str`.")
    sys.exit(0)

changed = []
errors = []
for p in repo_path.glob(glob):
    if not p.is_file() or ".git" in p.parts:
        continue
    try:
        src = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    try:
        new = transform(src, str(p.relative_to(repo_path)))
    except Exception as e:
        errors.append(f"{p}: {e}")
        continue
    if new is not None and new != src:
        p.write_text(new, encoding="utf-8")
        changed.append(str(p.relative_to(repo_path)))

report = [f"changed: {len(changed)} file(s)"]
report += [f"  {c}" for c in changed[:50]]
if errors:
    report.append(f"errors: {len(errors)}")
    report += [f"  {e}" for e in errors[:20]]
print("\n".join(report))
'''


def _run_codemod_subprocess(repo_path: Path, script: str, glob: str) -> str:
    """Execute an LLM-authored codemod in an isolated child process.

    Isolation properties vs the old in-process ``exec``:
    - separate process → cannot corrupt/inspect the long-lived agent's memory;
    - scrubbed environment → no GITHUB_TOKEN / API keys leak into the script;
    - hard timeout → a runaway/infinite codemod can't hang the agent.
    """
    with tempfile.TemporaryDirectory() as td:
        script_file = Path(td) / "codemod_script.py"
        harness_file = Path(td) / "codemod_harness.py"
        script_file.write_text(script, encoding="utf-8")
        harness_file.write_text(_CODEMOD_HARNESS, encoding="utf-8")

        # Minimal env: keep PATH/HOME (and PYTHONPATH if the agent set one) so
        # the child can still import libcst etc. from site-packages, but drop
        # every secret-bearing variable.
        safe_env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        if "PYTHONPATH" in os.environ:
            safe_env["PYTHONPATH"] = os.environ["PYTHONPATH"]

        try:
            proc = subprocess.run(
                [sys.executable, str(harness_file), str(script_file), str(repo_path), glob],
                capture_output=True, text=True, timeout=300, env=safe_env, cwd=str(repo_path),
            )
        except subprocess.TimeoutExpired:
            return "codemod timed out after 300s (aborted, no further changes)."
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 and not out:
            return f"codemod failed [exit {proc.returncode}]:\n{(proc.stderr or '')[-2000:]}"
        return out


# =================================================================
#                            DETECTORS
# =================================================================

def _autodetect_test_cmd(repo_path: Path) -> str:
    if (repo_path / "pyproject.toml").exists() or (repo_path / "pytest.ini").exists():
        return "pytest -q"
    if (repo_path / "package.json").exists():
        return "npm test --silent || yarn test"
    if (repo_path / "go.mod").exists():
        return "go test ./..."
    if (repo_path / "Cargo.toml").exists():
        return "cargo test --quiet"
    if (repo_path / "Makefile").exists():
        return "make test"
    return ""


def _autodetect_lint_cmd(repo_path: Path, fix: bool = False) -> list[str]:
    cmds: list[str] = []
    if (repo_path / "pyproject.toml").exists() or list(repo_path.glob("*.py")):
        cmds.append("ruff check --fix ." if fix else "ruff check .")
    if (repo_path / "package.json").exists():
        cmds.append("npx --no-install eslint --fix ." if fix else "npx --no-install eslint .")
    if (repo_path / "go.mod").exists():
        cmds.append("golangci-lint run --fix ./..." if fix else "golangci-lint run ./...")
    if (repo_path / "Cargo.toml").exists():
        cmds.append("cargo clippy --fix --allow-dirty --allow-staged" if fix else "cargo clippy")
    return cmds


def _autodetect_fmt_cmd(repo_path: Path) -> str:
    if (repo_path / "pyproject.toml").exists() or list(repo_path.glob("*.py")):
        return "ruff format ."
    if (repo_path / "package.json").exists():
        return "npx --no-install prettier --write ."
    if (repo_path / "go.mod").exists():
        return "gofmt -w ."
    if (repo_path / "Cargo.toml").exists():
        return "cargo fmt"
    return ""


def _autodetect_audit_cmd(repo_path: Path) -> str:
    if (repo_path / "pyproject.toml").exists() or (repo_path / "requirements.txt").exists():
        return "pip-audit --strict || true"
    if (repo_path / "package.json").exists():
        return "npm audit --omit=dev --json || true"
    if (repo_path / "Cargo.toml").exists():
        return "cargo audit || true"
    return ""


def _detect_runner(text: str) -> str:
    if "pytest" in text or "test_" in text or " failed" in text and " passed" in text:
        return "pytest"
    if "PASS " in text or "FAIL " in text or "describe(" in text:
        return "jest"
    if "ok\t" in text or "--- FAIL:" in text:
        return "go test"
    if "running" in text and "test result:" in text:
        return "cargo test"
    return "unknown"


def _branch_exists(repo_path: Path, branch: str) -> bool:
    try:
        subprocess.check_output(
            ["git", "rev-parse", "--verify", branch], cwd=repo_path, stderr=subprocess.DEVNULL
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _find_open_pr_for_branch(gh: GitHubOps, repo_full_name: str, branch: str):
    repo = gh.get_repo(repo_full_name)
    pulls = repo.get_pulls(state="open", head=f"{repo.owner.login}:{branch}")
    for pr in pulls:
        return pr
    return None
