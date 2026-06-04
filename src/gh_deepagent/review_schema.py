"""Structured response schema for the `reviewer` sub-agent.

Using Pydantic + deepagents' `response_format` field on the SubAgent spec means
the reviewer returns *parsed* data instead of raw markdown — so:

  - the runner can render a consistent, well-formatted GitHub PR comment;
  - downstream tooling (CI gates, dashboards) can consume reviewer output
    programmatically;
  - we get free LLM-side validation: malformed output triggers a retry by the
    deep-agents harness (ToolStrategy / AutoStrategy under the hood).

If the user's LangChain version is too old to support the per-subagent
`response_format` field, we fall back to free-form text — see `agent.py`.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


Severity = Literal["blocking", "suggestion", "nit"]


class ReviewFinding(BaseModel):
    """One specific observation made by the reviewer about the diff."""

    severity: Severity = Field(
        description="`blocking` = must fix before merging, "
                    "`suggestion` = should fix, "
                    "`nit` = optional polish."
    )
    category: Literal[
        "correctness", "style", "security", "performance",
        "tests", "documentation", "other",
    ] = Field(description="The aspect of the change the finding relates to.")
    file: Optional[str] = Field(
        default=None,
        description="Relative path to the file the finding is about, if scoped.",
    )
    line: Optional[int] = Field(
        default=None,
        description="1-indexed line number where the finding applies, if scoped.",
    )
    message: str = Field(
        description="One-paragraph explanation of the finding. Concrete + actionable."
    )
    suggested_patch: Optional[str] = Field(
        default=None,
        description="Optional `diff`-formatted suggestion to fix the finding.",
    )


class ReviewReport(BaseModel):
    """The reviewer sub-agent's full report."""

    verdict: Literal["approve", "request_changes", "comment"] = Field(
        description="`approve` if no blocking issues, `request_changes` if any "
                    "blocking finding, `comment` for FYI-only reviews."
    )
    summary: str = Field(
        description="A 1-2 sentence overall assessment of the diff."
    )
    findings: list[ReviewFinding] = Field(
        default_factory=list,
        description="Individual observations. Empty list when there's nothing to say.",
    )


# ---------------------------------------------------------------- formatter

_SEVERITY_BADGE = {
    "blocking":   "🛑 **blocking**",
    "suggestion": "💡 **suggestion**",
    "nit":        "🧹 _nit_",
}

_VERDICT_BADGE = {
    "approve":         "✅ **Approve**",
    "request_changes": "🚧 **Changes requested**",
    "comment":         "💬 **Comment**",
}


def render_report_markdown(report: ReviewReport) -> str:
    """Turn a ReviewReport into the body of a GitHub PR comment."""
    lines: list[str] = []
    lines.append(f"### 🤖 gh-deepagent review — {_VERDICT_BADGE[report.verdict]}")
    lines.append("")
    lines.append(report.summary.strip())
    lines.append("")

    if not report.findings:
        lines.append("_No specific findings._")
        return "\n".join(lines)

    # Group by severity (blocking first, then suggestion, then nit).
    by_sev: dict[str, list[ReviewFinding]] = {"blocking": [], "suggestion": [], "nit": []}
    for f in report.findings:
        by_sev.setdefault(f.severity, []).append(f)

    for sev in ("blocking", "suggestion", "nit"):
        items = by_sev.get(sev) or []
        if not items:
            continue
        lines.append(f"#### {_SEVERITY_BADGE[sev]} ({len(items)})")
        lines.append("")
        for f in items:
            loc = ""
            if f.file:
                loc = f" — `{f.file}`" + (f":{f.line}" if f.line else "")
            lines.append(f"- **[{f.category}]**{loc}  ")
            lines.append(f"  {f.message.strip()}")
            if f.suggested_patch:
                lines.append("")
                lines.append("  ```diff")
                for pl in f.suggested_patch.strip().splitlines():
                    lines.append("  " + pl)
                lines.append("  ```")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_report_plain(report: ReviewReport) -> str:
    """Fallback flat-text rendering (for log lines, CLI, etc.)."""
    parts = [f"[{report.verdict}] {report.summary}"]
    for f in report.findings:
        loc = f" ({f.file}:{f.line})" if f.file and f.line else (f" ({f.file})" if f.file else "")
        parts.append(f"  - {f.severity.upper()} [{f.category}]{loc}: {f.message}")
    return "\n".join(parts)
