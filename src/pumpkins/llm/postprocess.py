"""Stage 3 — LLM post-processing.

Takes clang-tidy diagnostics + diff context and produces triaged findings:

  1. noise filtering  — drop diagnostics irrelevant to the change / false positives
  2. severity rating  — critical..info, judged in context
  3. human explanation & fix proposal per surviving finding
  4. extra findings   — problems the active profile asks for that clang-tidy
     cannot detect, plus violations of the repository's own approved rules

The system prompt is assembled per run from three parts (build_system_prompt):
base instructions, the profile's focus, and the repo's rules. Rules the
deterministic checker already handles are listed but marked "do not report", so
the two producers do not double up on the same violation.

The provider (Claude / GPT) is selected by LLM_PROVIDER via llm/provider.py;
auth is each SDK reading its own key from the environment — keys are never
passed around in code.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from pumpkins.config import REVIEW_TEMPERATURE, default_review_model
from pumpkins.llm.provider import get_client
from pumpkins.profiles import DEFAULT_PROFILE, Profile, get_profile
from pumpkins.models import (
    DetectorKind,
    DiffScope,
    Evidence,
    Finding,
    RawDiagnostic,
    Severity,
)

log = logging.getLogger(__name__)

_BASE_PROMPT = """\
You are a senior C++ reviewer. You receive: (a) a git diff of C++ changes, and
(b) clang-tidy diagnostics restricted to the changed lines. The analysis ran
WITHOUT building the project, so diagnostics may be low-confidence; the diff is
the ground truth.

Your tasks:
1. Triage each numbered diagnostic: keep it only if it points at a plausible,
   change-related defect. Drop style noise, false positives caused by missing
   compile flags, and issues in unchanged code.
2. For each kept diagnostic, assign a severity (critical/high/medium/low/info),
   write a short title, a concise explanation of the concrete failure scenario,
   and a suggested fix (include a small code snippet when useful).
3. Independently scan the diff for the problems described under FOCUS, and for
   violations of the repository rules if any are listed. Report these as extra
   findings with a file and (new-side) line number from the diff.

Be conservative with extra findings: only report what the diff actually shows.
Explanations must name the concrete failure, not generic advice.
"""

_RULES_PREAMBLE = """\
## Repository rules

Rules this project has adopted and a human has approved. They are the review's
criteria — prefer reporting a rule violation over an opinion of your own, and
set `rule_id` on any finding that rests on one.
"""

_MACHINE_CHECKED_NOTE = """\
Already checked mechanically — do NOT report these, they would be duplicates.
They are listed so you understand the project's style:
"""

_LLM_CHECKED_NOTE = """\
Only you can check these (they cannot be expressed as a mechanical pattern).
Judge the diff against each one:
"""


def build_system_prompt(
    profile: Profile,
    rules: list | None = None,
    cxx_standard: object | None = None,
) -> str:
    """Assemble the system prompt from three parts.

    Base instructions + the profile's focus + the repository's own rules. It was
    one hardcoded concurrency string, which is why a real PR whose defect was a
    C++17 construct in a C++11 library came back with an empty verdict: the
    model was never asked about anything else.
    """
    parts = [_BASE_PROMPT]

    if cxx_standard is not None:
        parts.append(
            f"## Project's minimum C++ standard\n\n"
            f"C++{cxx_standard.minimum}, declared in: {', '.join(cxx_standard.sources)}.\n"
            f"Anything requiring a newer standard breaks this project's own CI.\n"
        )
    elif profile.needs_cxx_standard:
        parts.append(
            "## Project's minimum C++ standard\n\n"
            "NOT DECLARED anywhere this tool could read. Do not guess one — "
            "report a standard-level problem only if the diff itself makes the "
            "requirement explicit.\n"
        )

    parts.append(f"## FOCUS\n\n{profile.llm_focus}\n")

    if rules:
        machine, llm_judged = _split_rules(rules)
        section = [_RULES_PREAMBLE]
        if machine:
            section.append(_MACHINE_CHECKED_NOTE + _format_rules(machine))
        if llm_judged:
            section.append(_LLM_CHECKED_NOTE + _format_rules(llm_judged))
        parts.append("\n".join(section))

    return "\n".join(parts)


def _split_rules(rules: list) -> tuple[list, list]:
    """Rules the deterministic checker already handles vs. the rest.

    Without this split the model re-reports every naming violation the regex
    checker just reported, and the user sees each one twice.
    """
    machine = [r for r in rules if r.facet in ("prefix", "suffix", "casing")]
    return machine, [r for r in rules if r not in machine]


def _format_rules(rules: list) -> str:
    lines = []
    for rule in rules:
        scope = "" if rule.scope.is_repo_wide else f" [적용 범위: {rule.scope.describe()}]"
        lines.append(f"- `{rule.id}` ({rule.category}): {rule.description}{scope}")
    return "\n".join(lines) + "\n"


class _Verdict(BaseModel):
    """LLM triage verdict for one numbered clang-tidy diagnostic."""

    index: int = Field(description="0-based index of the diagnostic being triaged")
    keep: bool
    severity: Severity = Severity.medium
    title: str = ""
    explanation: str = ""
    suggestion: str = ""


class _ExtraFinding(BaseModel):
    """A problem the LLM found directly in the diff."""

    file: str
    line: int
    severity: Severity
    title: str
    explanation: str
    suggestion: str = ""
    rule_id: str | None = Field(
        default=None,
        description="id of the repository rule this violates, or null if none applies",
    )


class _LlmReview(BaseModel):
    verdicts: list[_Verdict]
    extra_findings: list[_ExtraFinding] = Field(default_factory=list)


class LlmPostProcessor:
    def __init__(self, model: str | None = None, profile: str = DEFAULT_PROFILE):
        self.profile: Profile = get_profile(profile)
        self.model = model or default_review_model()
        self.client = get_client()  # provider from LLM_PROVIDER; key from env
        # Kept for `--out-dir`: "왜 이런 지적을 했지" 또는 "왜 아무 말도 안 하지"의
        # 답은 대개 프롬프트에 그대로 적혀 있다.
        self.last_request: str | None = None
        self.last_response: dict | None = None

    def process(
        self,
        scope: DiffScope,
        diagnostics: list[RawDiagnostic],
        shallow_mode: bool,
        rules: list | None = None,
        cxx_standard: object | None = None,
    ) -> tuple[list[Finding], int]:
        """Returns (findings, dropped_as_noise_count)."""
        system_prompt = build_system_prompt(self.profile, rules, cxx_standard)
        user_prompt = self._build_prompt(scope, diagnostics, shallow_mode)
        self.last_request = (
            f"=== model ===\n{self.model} (profile: {self.profile.name}, "
            f"temperature: {REVIEW_TEMPERATURE})\n\n"
            f"=== system ===\n{system_prompt}\n"
            f"=== user ===\n{user_prompt}\n"
        )
        result = self.client.parse(
            model=self.model,
            max_tokens=16000,
            system=system_prompt,
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
        return self._to_findings(review, diagnostics, {r.id for r in rules or []})

    # ------------------------------------------------------------- internals

    def _verified_rule_id(self, rule_id: str | None, known: set[str]) -> str | None:
        """Only an id the user actually approved may appear as a finding's basis.

        The model is asked to cite a rule, and a model asked for an id will
        sometimes produce a plausible-looking one that was never approved.
        Trusting it would make the provenance label lie — the one thing it
        exists to prevent — so an unknown id is dropped and the finding stands
        on the model's own judgement, which is what it actually is.
        """
        if not rule_id:
            return None
        if rule_id not in known:
            log.warning(
                "LLM cited unknown rule %r — recorded as an unbacked finding", rule_id
            )
            return None
        return rule_id

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
        self,
        review: _LlmReview,
        diagnostics: list[RawDiagnostic],
        known_rule_ids: set[str],
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
                    # Judged by the model, so never reproducible — but when it
                    # rests on an approved rule, that rule is recorded as the
                    # basis. Rule-backed and free-judgement findings are then
                    # distinguishable in the report and in findings.json.
                    evidence=Evidence(
                        detector=DetectorKind.llm,
                        rule_id=self._verified_rule_id(e.rule_id, known_rule_ids),
                        model=self.model,
                    ),
                )
            )
        order = list(Severity)
        findings.sort(key=lambda f: (order.index(f.severity), f.file, f.line))
        return findings, dropped
