"""Convention learning, inference path — AI-inferred repo-local rules (opt-in).

The statistics path (extractor + learner) can only surface conventions that fit
a template it was given: prefix / suffix / casing. That template is the tool
author's prior, so it finds "conventions the tool knows how to look for", not
necessarily the ones THIS repo's authors care about (docs
convention-detection-design.md — repo-specific rules).

This module does the other thing: it hands the model real code with NO facet
template and asks it to *infer* the conventions THIS repo follows that are not
universal C++. The default path measures; this one guesses. Measured on a
fixture it re-found the `m_` prefix rule unprompted AND inferred `#pragma once`
— a convention the facet system cannot express at all.

Two deliberate boundaries keep it honest, both agreed in the design discussion:

- Inference is open, adoption is not. An inferred rule is a GUESS, not a
  measured rule: it has no verified coverage, so it is written as a
  facet="other" candidate that the deterministic checker skips and a human must
  approve (candidates/ → rules/). Once approved it rides the existing 방안 B
  path — the review LLM judges the diff against it, labelled reproducible=False.
  It never gates CI on its own.
- Cost is opt-in. This sends source code, so tokens scale with repo size, unlike
  the statistics path whose input stays small. Hence a flag, and a char cap.

The mechanical-verification step ("does this inferred rule actually hold at 85%")
is the next increment; until then these are LLM-judged, not reproducible.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from pumpkins.config import LEARN_TEMPERATURE, default_reasoning_model
from pumpkins.conventions.extractor import select_files
from pumpkins.conventions.learner import ConventionRule
from pumpkins.conventions.scope import RuleScope
from pumpkins.llm.provider import get_client

log = logging.getLogger(__name__)

# Source sent to the model is capped so an opt-in run has a bounded cost. Files
# are taken in path order until the budget is spent; the log says how many were
# included so a truncated scan is visible rather than silent.
MAX_INFER_CHARS = 40_000

_UNSAFE_ID = re.compile(r"[^a-z0-9]+")

_INFER_SYSTEM = """\
You are analyzing a C++ repository to INFER its PROJECT-SPECIFIC conventions:
patterns THIS codebase follows consistently that are NOT universal C++ rules a
generic linter already enforces. Look BEYOND naming — also structural / API
shape / ownership (raw vs smart pointers) / const-correctness / error handling /
file layout — but include naming too when it is clearly consistent.

For each convention you infer:
- rule: ONE sentence, in Korean, phrased so it can directly back a review
  comment (e.g. "헤더는 include 가드 대신 `#pragma once`를 쓴다").
- kind: one of naming | structural | api-shape | ownership | const |
  error-handling | layout | other
- evidence: the concrete thing in the code that made you say it (names, files).
- check: a STRUCTURED, machine-runnable check we use to VERIFY your guess
  against the whole repo. Fill it ONLY when the rule truly reduces to one of:
    * naming — {kind: "naming", category: member|function|class_type|constant,
      facet: prefix|suffix|casing, value: the EXACT string or casing style}.
      Allowed casing values: lowerCamel, lower_snake, UpperCamel, UPPER_SNAKE.
      value for prefix/suffix is the literal affix, e.g. "m_", "_", "Impl".
    * header_directive — {kind: "header_directive", text: the exact first line,
      e.g. "#pragma once"}.
  Otherwise set {kind: "none"}. A wrong check gets the rule REJECTED when its
  measured coverage is low, so only fill it when the rule genuinely reduces to
  that check across ALL of the category (not a subset you cannot express).
  Worked examples — make the check match the rule EXACTLY:
    "멤버 변수는 m_ 접두사를 쓴다" → {kind: naming, category: member,
       facet: prefix, value: "m_"}   (value is the literal affix WITH the
       underscore — "m_", never "m")
    "함수 이름은 lowerCamel" → {kind: naming, category: function,
       facet: casing, value: "lowerCamel"}
    "헤더는 #pragma once로 시작한다" → {kind: header_directive,
       text: "#pragma once"}

Be conservative: only patterns you actually see repeated. Do not restate
universal C++ or anything a generic linter owns. Returning few rules is fine.
"""


class RuleCheck(BaseModel):
    """A machine-executable check attached to an inferred rule.

    kind="none" means the rule is not expressible in today's check vocabulary,
    so it stays an LLM-judged guess (facet=other, reproducible=False). The other
    kinds are run by conventions/verifier.py to measure real coverage."""

    kind: Literal["naming", "header_directive", "none"] = "none"
    # naming
    category: str = ""   # member | function | class_type | constant
    facet: Literal["prefix", "suffix", "casing", ""] = ""
    value: str = ""
    # header_directive
    text: str = ""


class InferredRule(BaseModel):
    """One convention the model inferred from reading the code (a guess)."""

    rule: str
    kind: str = "other"
    evidence: str = ""
    mechanical_check: str = ""
    checkable_with_ast: bool = False
    check: RuleCheck = Field(default_factory=RuleCheck)


class InferredRuleSet(BaseModel):
    rules: list[InferredRule] = Field(default_factory=list)


@dataclass
class InferOutcome:
    rules: list[InferredRule]
    input_tokens: int
    output_tokens: int
    files_read: int
    truncated: bool

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _gather_code(
    repo: Path,
    include: list[str] | None,
    exclude: list[str] | None,
    include_tests: bool,
    max_chars: int,
) -> tuple[str, int, bool]:
    """Concatenate in-scope C++ sources up to a char budget.

    Reuses the learn scan's file selection so scoping / vendored-dir skipping /
    test exclusion behave exactly as the statistics path does."""
    files = select_files(repo, include, exclude, include_tests)
    parts: list[str] = []
    used = 0
    read = 0
    truncated = False
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.debug("skipping unreadable file %s: %s", path, exc)
            continue
        block = f"// ===== {path.relative_to(repo)} =====\n{text}"
        if used + len(block) > max_chars and parts:
            truncated = True
            break
        parts.append(block)
        used += len(block)
        read += 1
    return "\n\n".join(parts), read, truncated


class RuleInferrer:
    """Infers repo-local conventions by reading code, facet-free. Reasoning-heavy,
    so it runs on the strong (review-tier) model by default — the same tier §4.1
    assigned to inference."""

    def __init__(self, model: str | None = None):
        self.model = model or default_reasoning_model()
        self.client = get_client()

    def infer(
        self,
        repo: Path,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        include_tests: bool = False,
        max_chars: int = MAX_INFER_CHARS,
    ) -> InferOutcome:
        code, files_read, truncated = _gather_code(
            repo, include, exclude, include_tests, max_chars
        )
        if not code:
            return InferOutcome([], 0, 0, 0, False)
        if truncated:
            log.warning(
                "rule inference: repo exceeds %d char budget — inferred from "
                "first %d file(s) only", max_chars, files_read,
            )
        parsed = self.client.parse(
            model=self.model,
            max_tokens=2000,
            system=_INFER_SYSTEM,
            user=f"Repository C++ source:\n\n{code}",
            temperature=LEARN_TEMPERATURE,
            schema=InferredRuleSet,
        )
        result = parsed.parsed
        rules = result.rules if result else []
        log.info(
            "rule inference: %d rule(s) from %d file(s); tokens in=%d out=%d",
            len(rules), files_read, parsed.input_tokens, parsed.output_tokens,
        )
        return InferOutcome(
            rules=rules,
            input_tokens=parsed.input_tokens,
            output_tokens=parsed.output_tokens,
            files_read=files_read,
            truncated=truncated,
        )


def _inferred_id(rule_text: str, taken: set[str]) -> str:
    """Stable, unique, filesystem-safe id from the rule text.

    Deterministic so a re-run inferring the same rule reconciles to the same
    candidate (reconcile dedups by id) instead of piling up duplicates. The
    `ai-` prefix marks it as an AI-inferred guess, not a measured rule."""
    words = _UNSAFE_ID.sub("-", rule_text.lower()).strip("-").split("-")
    base = "ai-" + "-".join(w for w in words if w)[:48].strip("-") or "ai-inferred"
    candidate = base
    n = 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    taken.add(candidate)
    return candidate


def to_convention_rules(
    inferred: list[InferredRule], scan_scope: RuleScope | None = None
) -> list[ConventionRule]:
    """Turn AI-inferred rules into facet="other" candidate rules.

    coverage/occurrences are 0 on purpose: these are guesses, NOT statistically
    measured, so they carry no real numbers and must bypass the threshold gate
    (which is for measured rules) and land in candidates/ for a human. The
    evidence is kept in `examples`; the check hint stays in the console for now.
    """
    scope = scan_scope or RuleScope()
    taken: set[str] = set()
    rules: list[ConventionRule] = []
    for r in inferred:
        rule_text = r.rule.strip()
        if not rule_text:
            continue
        rules.append(
            ConventionRule(
                id=_inferred_id(rule_text, taken),
                category=r.kind or "other",
                description=rule_text,
                facet="other",
                value="",
                coverage=0.0,
                occurrences=0,
                confidence="low",
                examples=[r.evidence] if r.evidence else [],
                scope=scope.model_copy(deep=True),
            )
        )
    return rules
