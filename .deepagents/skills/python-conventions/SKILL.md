---
name: python-conventions
description: "Python style conventions: ruff format/lint, type hints, import order, error handling, async rules, src/ layout. Use when the target repo is Python (pyproject.toml or .py files present)."
license: MIT
---

# Skill: python-conventions

Apply only when the target repo is Python. Override with repo-local `AGENTS.md`
or `pyproject.toml` settings.

## Style
- `ruff format` is the source of truth — never argue with the formatter.
- Line length: project `pyproject.toml` `[tool.ruff] line-length`, else 100.
- Type hints: yes, even in tests. `from __future__ import annotations` at top.
- Avoid `Any`. Prefer `Protocol` for duck typing, `TypedDict` for JSON-ish.
- f-strings > `.format()` > `%`. Never `+` for >2 strings.

## Imports
- Order: stdlib → third-party → local, blank line between groups.
- No wildcard imports. No relative `from .. import x` beyond 2 levels.
- Move heavy imports inside functions if used in <50% of calls (lazy).

## Errors
- Raise specific exceptions; never bare `except:`.
- Catch `except Exception as e:` only at boundaries, log+re-raise or convert.
- Don't suppress with `except: pass`. Use `contextlib.suppress(SpecificError)`.

## Async
- Don't mix sync DB calls inside async handlers; `run_in_executor` if needed.
- Use `asyncio.gather(*, return_exceptions=True)` for fan-out + handle errors.
- No `time.sleep` in async code — `await asyncio.sleep`.

## Project layout
```
src/<pkg>/
tests/
pyproject.toml
README.md
```
- Always use the `src/` layout if `pyproject.toml` is new.
- Keep `__init__.py` minimal: re-exports + `__version__`.

## CLI
- `typer` or `click` over `argparse` for new tools.
- `--help` must explain every flag (typer does this automatically).
- Exit codes: 0 OK, 1 user error, 2 internal error, 130 SIGINT.
