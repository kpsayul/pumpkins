"""Stage 3 — LLM post-processing.

Takes clang-tidy diagnostics + diff context and produces triaged findings:

  1. noise filtering  — drop diagnostics irrelevant to the change / false positives
  2. severity rating  — critical..info, judged in context
  3. human explanation & fix proposal per surviving finding
  4. extra findings   — concurrency anti-patterns visible in the diff that
     clang-tidy cannot detect (lock-order inversion, unguarded shared member
     access, volatile misused for synchronization)

The provider (Claude / GPT) is selected by LLM_PROVIDER via llm/provider.py;
auth is each SDK reading its own key from the environment — keys are never
passed around in code.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from pumpkins.config import REVIEW_TEMPERATURE, default_review_model
from pumpkins.llm.provider import get_client
from pumpkins.models import (
    DetectorKind,
    DiffScope,
    Evidence,
    Finding,
    RawDiagnostic,
    Severity,
)

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
    def __init__(self, model: str | None = None):
        self.model = model or default_review_model()
        self.client = get_client()  # provider from LLM_PROVIDER; key from env
        # Kept for `--out-dir`: "왜 이런 지적을 했지" 또는 "왜 아무 말도 안 하지"의
        # 답은 대개 프롬프트에 그대로 적혀 있다.
        self.last_request: str | None = None
        self.last_response: dict | None = None

    def process(
        self, scope: DiffScope, diagnostics: list[RawDiagnostic], shallow_mode: bool
    ) -> tuple[list[Finding], int]:
        """Returns (findings, dropped_as_noise_count)."""
        user_prompt = self._build_prompt(scope, diagnostics, shallow_mode)
        self.last_request = (
            f"=== model ===\n{self.model}\n\n"
            f"=== system ===\n{_SYSTEM_PROMPT}\n"
            f"=== user ===\n{user_prompt}\n"
        )
        result = self.client.parse(
            model=self.model,
            max_tokens=16000,
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            schema=_LlmReview,
            temperature=REVIEW_TEMPERATURE,
        )
        review = result.parsed
        if review is None:
            raise RuntimeError("LLM returned no parseable review output")
        self.last_response = review.model_dump()
        log.info(
            "LLM triage: %d/%d diagnostics kept, %d extra finding(s); tokens in=%d out=%d",
            sum(v.keep for v in review.verdicts),
            len(diagnostics),
            len(review.extra_findings),
            result.input_tokens,
            result.output_tokens,
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
                    severity=v.severity,
                    title=v.title or d.message,
                    explanation=v.explanation or d.message,
                    suggestion=v.suggestion,
                    # The defect is clang-tidy's, but its *presence in the
                    # report* is not: the model decided to keep it, and a
                    # re-run may drop it. So the rule id stays, and
                    # reproducible is forced off — the same diagnostic reported
                    # by `--no-llm` is reproducible, this one is not.
                    evidence=Evidence(
                        detector=DetectorKind.clang_tidy,
                        rule_id=d.check or None,
                        reproducible=False,
                        model=self.model,
                    ),
                )
            )
        for e in review.extra_findings:
            findings.append(
                Finding(
                    file=e.file,
                    line=e.line,
                    severity=e.severity,
                    title=e.title,
                    explanation=e.explanation,
                    suggestion=e.suggestion,
                    # Nothing but the model's judgement backs these, so they
                    # carry no rule id and are marked non-reproducible.
                    evidence=Evidence(detector=DetectorKind.llm, model=self.model),
                )
            )
        order = list(Severity)
        findings.sort(key=lambda f: (order.index(f.severity), f.file, f.line))
        return findings, dropped
