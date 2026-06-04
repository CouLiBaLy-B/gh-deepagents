"""ReviewReport / ReviewFinding rendering."""
from __future__ import annotations

import pytest

from gh_deepagent.review_schema import (
    ReviewFinding,
    ReviewReport,
    render_report_markdown,
    render_report_plain,
)


def _sample_report():
    return ReviewReport(
        verdict="request_changes",
        summary="Two correctness issues, one nit.",
        findings=[
            ReviewFinding(severity="blocking", category="correctness",
                          file="src/x.py", line=42,
                          message="`compute()` divides by `n` without checking for zero."),
            ReviewFinding(severity="suggestion", category="tests",
                          message="No regression test for the new edge case."),
            ReviewFinding(severity="nit", category="style",
                          file="src/x.py",
                          message="Trailing whitespace on a blank line."),
        ],
    )


def test_markdown_render_has_sections():
    out = render_report_markdown(_sample_report())
    assert "Changes requested" in out
    assert "blocking" in out and "suggestion" in out and "nit" in out
    assert "src/x.py" in out and "42" in out
    assert "compute()" in out


def test_markdown_render_no_findings():
    r = ReviewReport(verdict="approve", summary="LGTM", findings=[])
    out = render_report_markdown(r)
    assert "Approve" in out
    assert "No specific findings" in out


def test_plain_render_is_flat():
    out = render_report_plain(_sample_report())
    assert "[request_changes]" in out
    assert "BLOCKING" in out


def test_severity_validation():
    with pytest.raises(Exception):   # pydantic ValidationError or similar
        ReviewFinding(severity="lol", category="style", message="x")


def test_verdict_validation():
    with pytest.raises(Exception):
        ReviewReport(verdict="approveeee", summary="x")


def test_findings_default_empty():
    r = ReviewReport(verdict="comment", summary="just observing")
    assert r.findings == []
