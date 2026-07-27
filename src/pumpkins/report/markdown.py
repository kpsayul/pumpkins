"""Stage 4 — markdown report rendering."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from pumpkins.models import DetectorKind, Evidence, ReviewResult, Severity

_SEVERITY_EMOJI = {
    Severity.critical: "🟥",
    Severity.high: "🟧",
    Severity.medium: "🟨",
    Severity.low: "🟦",
    Severity.info: "⬜",
}


def _evidence_line(evidence: Evidence) -> str:
    """One line answering 'why did it say this, and would it say it again?'."""
    parts = [f"규칙 `{evidence.rule_id}`" if evidence.rule_id else "규칙 없음"]
    who = evidence.detector.value
    if evidence.detector is DetectorKind.llm and evidence.model:
        who = f"{who}(`{evidence.model}`)"
    parts.append(who)
    parts.append("재현 가능" if evidence.reproducible else "모델 의존 — 재현 보장 안 됨")
    if evidence.occurrences is not None and evidence.coverage is not None:
        parts.append(f"근거 {evidence.occurrences}개 중 {evidence.coverage:.0%}")
    if evidence.rule_scope and evidence.rule_scope != "리포 전체":
        parts.append(f"적용 {evidence.rule_scope}")
    return " · ".join(parts)


def _coverage_section(result: ReviewResult) -> list[str]:
    """List what the run did not examine, and why.

    Silence and a pass look identical unless the gaps are named. Both gaps here
    are structural rather than incidental: a non-C++ extension is dropped at
    diff collection, and without a compile DB headers cannot be analyzed at
    all — which in a header-only project means the entire implementation.
    """
    if not result.has_coverage_gap:
        return []

    lines = ["## ⚠️ 검사하지 못한 변경", ""]
    if result.skipped_non_cpp:
        lines.append(
            f"**C++ 파일이 아니어서 읽지 않음** ({len(result.skipped_non_cpp)}개) — "
            "이 파일의 변경은 어떤 단계도 보지 않았습니다."
        )
        lines.append("")
        lines += [f"- `{p}`" for p in result.skipped_non_cpp]
        lines.append("")
    if result.skipped_headers:
        lines.append(
            f"**정적 분석 불가** ({len(result.skipped_headers)}개) — "
            "compile_commands.json이 없어 헤더를 단독 분석할 수 없습니다. "
            "컨벤션 대조와 LLM 리뷰는 이 파일에도 적용됩니다."
        )
        lines.append("")
        lines += [f"- `{p}`" for p in result.skipped_headers]
        lines.append("")
    return lines


def render_markdown(result: ReviewResult) -> str:
    lines: list[str] = []
    lines.append("# C++ Review Report")
    lines.append("")
    lines.append(f"- generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"- diff base: `{result.base_ref or 'working tree (HEAD)'}`")
    lines.append(f"- check profile: `{result.profile}`")
    mode = "shallow (no compile_commands.json — lower confidence)" if result.shallow_mode else "compile-DB"
    lines.append(f"- analysis mode: {mode}")
    llm = (
        f"{result.provider or '?'} / `{result.model or '?'}`"
        if result.llm_used
        else "no (raw clang-tidy output, untriaged)"
    )
    lines.append(f"- LLM triage: {llm}")
    conv = (
        f"{result.conventions_loaded} active rule(s)"
        if result.conventions_loaded
        else "not loaded"
    )
    if result.conventions_pending:
        conv += (
            f" — {result.conventions_pending} candidate(s) awaiting approval, "
            f"not enforced"
        )
    lines.append(f"- conventions: {conv}")
    changed_cpp = result.analyzed_files + len(result.skipped_headers)
    lines.append(
        f"- coverage: {result.analyzed_files}/{changed_cpp} changed C++ file(s) "
        f"statically analyzed"
        + (f", {len(result.skipped_non_cpp)} non-C++ file(s) not read" if result.skipped_non_cpp else "")
    )
    lines.append(
        f"- diagnostics: {result.total_diagnostics} raw → "
        f"{result.dropped_as_noise} dropped as noise → {len(result.findings)} finding(s)"
    )
    lines.append("")

    lines.extend(_coverage_section(result))

    if not result.findings:
        if result.has_coverage_gap:
            # Never a green check when part of the change went unread: an
            # unqualified pass on an unanalyzed diff is worse than no report.
            lines.append(
                "⚠️ **분석된 범위에서는 지적이 없습니다** — 다만 위 파일들은 "
                "검사하지 못했으므로 이 결과를 통과로 읽지 마세요."
            )
        else:
            lines.append("✅ **No findings in the changed lines.**")
        lines.append("")
        return "\n".join(lines)

    counts = Counter(f.severity for f in result.findings)
    summary = " · ".join(
        f"{_SEVERITY_EMOJI[s]} {s.value}: {counts[s]}" for s in Severity if counts[s]
    )
    lines.append(f"**Summary:** {summary}")

    # The split that decides what may gate CI. Without it a model-dependent
    # finding and a deterministic one are indistinguishable in the report.
    stable = sum(1 for f in result.findings if f.evidence.reproducible)
    unstable = len(result.findings) - stable
    lines.append(
        f"**재현성:** 재현 가능 {stable}건 (CI 게이트로 사용 가능) · "
        f"모델 의존 {unstable}건 (다시 돌리면 달라질 수 있음 — 참고용)"
    )
    lines.append("")

    for i, f in enumerate(result.findings, 1):
        lines.append(f"## {i}. {_SEVERITY_EMOJI[f.severity]} [{f.severity.value}] {f.title}")
        lines.append("")
        lines.append(f"`{f.file}:{f.line}` — {_evidence_line(f.evidence)}")
        lines.append("")
        lines.append(f.explanation)
        if f.suggestion:
            lines.append("")
            lines.append("**Suggested fix:**")
            lines.append("")
            lines.append(f.suggestion)
        lines.append("")

    return "\n".join(lines)
