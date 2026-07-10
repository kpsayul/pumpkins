"""Stage 4 — markdown report rendering."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from pumpkins.models import ReviewResult, Severity

_SEVERITY_EMOJI = {
    Severity.critical: "🟥",
    Severity.high: "🟧",
    Severity.medium: "🟨",
    Severity.low: "🟦",
    Severity.info: "⬜",
}


def render_markdown(result: ReviewResult) -> str:
    lines: list[str] = []
    lines.append("# C++ Review Report")
    lines.append("")
    lines.append(f"- generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"- diff base: `{result.base_ref or 'working tree (HEAD)'}`")
    lines.append(f"- check profile: `{result.profile}`")
    mode = "shallow (no compile_commands.json — lower confidence)" if result.shallow_mode else "compile-DB"
    lines.append(f"- analysis mode: {mode}")
    llm = "yes" if result.llm_used else "no (raw clang-tidy output, untriaged)"
    lines.append(f"- LLM triage: {llm}")
    lines.append(
        f"- diagnostics: {result.total_diagnostics} raw → "
        f"{result.dropped_as_noise} dropped as noise → {len(result.findings)} finding(s)"
    )
    lines.append("")

    if not result.findings:
        lines.append("✅ **No findings in the changed lines.**")
        lines.append("")
        return "\n".join(lines)

    counts = Counter(f.severity for f in result.findings)
    summary = " · ".join(
        f"{_SEVERITY_EMOJI[s]} {s.value}: {counts[s]}" for s in Severity if counts[s]
    )
    lines.append(f"**Summary:** {summary}")
    lines.append("")

    for i, f in enumerate(result.findings, 1):
        lines.append(f"## {i}. {_SEVERITY_EMOJI[f.severity]} [{f.severity.value}] {f.title}")
        lines.append("")
        lines.append(f"`{f.file}:{f.line}` — `{f.check}` (source: {f.source})")
        lines.append("")
        lines.append(f.explanation)
        if f.suggestion:
            lines.append("")
            lines.append("**Suggested fix:**")
            lines.append("")
            lines.append(f.suggestion)
        lines.append("")

    return "\n".join(lines)
