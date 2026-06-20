"""Tests for the human-in-the-loop approval helper used by interactive runs."""
from __future__ import annotations

from types import SimpleNamespace

from gh_deepagent.runner import _prompt_decisions


def _interrupt(actions):
    return (SimpleNamespace(value={"action_requests": actions}),)


def test_empty_input_approves():
    intr = _interrupt([{"name": "finalize_patch", "args": {"b": "x"}}])
    assert _prompt_decisions(intr, input_fn=lambda _p: "") == [{"type": "approve"}]


def test_no_defaults_to_reject():
    intr = _interrupt([{"name": "codemod_python", "args": {}}])
    assert _prompt_decisions(intr, input_fn=lambda _p: "n") == [{"type": "reject"}]


def test_one_decision_per_action():
    intr = _interrupt([
        {"name": "a", "args": {}},
        {"name": "b", "args": {}},
    ])
    answers = iter(["y", "no"])
    out = _prompt_decisions(intr, input_fn=lambda _p: next(answers))
    assert out == [{"type": "approve"}, {"type": "reject"}]


def test_no_actions_yields_single_approve():
    intr = (SimpleNamespace(value={"action_requests": []}),)
    assert _prompt_decisions(intr, input_fn=lambda _p: "n") == [{"type": "approve"}]


def test_eof_on_stdin_approves():
    def raise_eof(_p):
        raise EOFError
    intr = _interrupt([{"name": "finalize_patch", "args": {}}])
    assert _prompt_decisions(intr, input_fn=raise_eof) == [{"type": "approve"}]
