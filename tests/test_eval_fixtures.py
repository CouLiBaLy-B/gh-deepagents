"""Keep the eval fixtures honest: each must be a sound eval target.

Runs the hermetic `--check-fixtures` logic (no LLM): the starting repo must
behave as declared, and the reference solution must make every check pass.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_EVAL = Path(__file__).resolve().parents[1] / "scripts" / "eval_agent.py"


def _load_eval_module():
    spec = importlib.util.spec_from_file_location("eval_agent", _EVAL)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses.field() can resolve the module.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_eval_module_loads_and_has_cases():
    mod = _load_eval_module()
    assert mod.CASES, "no eval cases defined"


@pytest.mark.parametrize("idx", range(len(_load_eval_module().CASES)))
def test_each_fixture_is_sound(idx):
    mod = _load_eval_module()
    case = mod.CASES[idx]
    [result] = mod.check_fixtures([case])
    assert result["sound"], f"fixture {case.name!r} is not sound: {result}"
