"""Stage 3 — LLM post-processing (Claude).

Takes clang-tidy diagnostics + diff context and produces triaged findings:

  1. noise filtering  — drop diagnostics irrelevant to the change / false positives
  2. severity rating  — critical..info, judged in context
  3. human explanation & fix proposal per surviving finding
  4. extra findings   — concurrency anti-patterns visible in the diff that
     clang-tidy cannot detect (lock-order inversion, unguarded shared member
     access, volatile misused for synchronization)

Auth: the anthropic SDK reads ANTHROPIC_API_KEY from the environment — the key
is never passed around in code.
"""

from __future__ import annotations

import logging

import anthropic
from pydantic import BaseModel, Field

from cpp_review_bot.config import DEFAULT_MODEL
from cpp_review_bot.models import DiffScope, Finding, RawDiagnostic, Severity

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a senior C++ reviewer specializing in concurrency bugs. You receive:
(a) a git diff of C++ changes, and (b) clang-tidy diagnostics restricted to the
changed lines. The analysis ran WITHOUT building the project, so diagnostics may
be low-confidence; the diff is the ground truth.

Your tasks:
1. Triage each numbered diagnostic: keep it only if it points at a plausible,
   change-related defect. Drop style noise, false positives caused by missing
   compile flags, and issues in unchanged code.
2. For each kept diagnostic, assign a severity (critical/high/medium/low/info),
   write a short title, a concise explanation of the concrete failure scenario,
   and a suggested fix (include a small code snippet when useful).
3. Independently scan the diff for concurrency anti-patterns clang-tidy misses:
   inconsistent lock acquisition order across code paths, shared members read or
   written without holding the guarding mutex, volatile used as a substitute for
   atomics/synchronization, condition variable waits without a predicate, and
   data published between threads without a happens-before edge. Report these as
   extra findings with a file and (new-side) line number from the diff.

Be conservative with extra findings: only report what the diff actually shows.
Explanations must name the concrete interleaving or failure, not generic advice.
"""


class _Verdict(BaseModel):
    """LLM triage verdict for one numbered clang-tidy diagnostic."""

    index: int = Field(description="0-based index of the diagnostic being triaged")
    keep: bool
    severity: Severity = Severity.medium
    title: str = ""
    explanation: str = ""
    suggestion: str = ""


class _ExtraFinding(BaseModel):
    """Concurrency issue the LLM found directly in the diff."""

    file: str
    line: int
    severity: Severity
    title: str
    explanation: str
    suggestion: str = ""


class _LlmReview(BaseModel):
    verdicts: list[_Verdict]
    extra_findings: list[_ExtraFinding] = Field(default_factory=list)


class LlmPostProcessor:
    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    def process(
        self, scope: DiffScope, diagnostics: list[RawDiagnostic], shallow_mode: bool
    ) -> tuple[list[Finding], int]:
        """Returns (findings, dropped_as_noise_count)."""
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": self._build_prompt(scope, diagnostics, shallow_mode)}],
            output_format=_LlmReview,
        )
        review = response.parsed_output
        if review is None:
            raise RuntimeError("LLM returned no parseable review output")
        log.info(
            "LLM triage: %d/%d diagnostics kept, %d extra finding(s); tokens in=%d out=%d",
            sum(v.keep for v in review.verdicts),
            len(diagnostics),
            len(review.extra_findings),
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        return self._to_findings(review, diagnostics)

    # ------------------------------------------------------------- internals

    def _build_prompt(
        self, scope: DiffScope, diagnostics: list[RawDiagnostic], shallow_mode: bool
    ) -> str:
        parts: list[str] = []
        if shallow_mode:
            parts.append(
                "NOTE: analysis ran in shallow mode (no compile_commands.json), "
                "so clang-tidy diagnostics are extra likely to be false positives.\n"
            )
        parts.append("## Git diff\n")
        for f in scope.files:
            parts.append(f"```diff\n{f.patch_text}\n```\n")
        parts.append("## clang-tidy diagnostics (numbered)\n")
        if diagnostics:
            for i, d in enumerate(diagnostics):
                parts.append(f"{i}. {d.file}:{d.line}:{d.column} [{d.check}] {d.level}: {d.message}")
        else:
            parts.append("(none — still perform task 3, the independent diff scan)")
        return "\n".join(parts)

    def _to_findings(
        self, review: _LlmReview, diagnostics: list[RawDiagnostic]
    ) -> tuple[list[Finding], int]:
        findings: list[Finding] = []
        dropped = 0
        for v in review.verdicts:
            if not (0 <= v.index < len(diagnostics)):
                log.warning("LLM verdict index %d out of range — ignored", v.index)
                continue
            if not v.keep:
                dropped += 1
                continue
            d = diagnostics[v.index]
            findings.append(
                Finding(
                    file=d.file,
                    line=d.line,
                    check=d.check,
                    severity=v.severity,
                    title=v.title or d.message,
                    explanation=v.explanation or d.message,
                    suggestion=v.suggestion,
                    source="clang-tidy",
                )
            )
        for e in review.extra_findings:
            findings.append(
                Finding(
                    file=e.file,
                    line=e.line,
                    check="llm-review",
                    severity=e.severity,
                    title=e.title,
                    explanation=e.explanation,
                    suggestion=e.suggestion,
                    source="llm",
                )
            )
        order = list(Severity)
        findings.sort(key=lambda f: (order.index(f.severity), f.file, f.line))
        return findings, dropped
