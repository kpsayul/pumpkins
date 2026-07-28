"""Mechanical verification of AI-inferred rules (the "scoring = code" half).

Inference (proposer.py) guesses conventions from reading code; a guess can be
wrong (measured live: "pointer members end in Ptr", inferred from one `m_pool`
that has no such suffix). This module is what makes a guess trustworthy: it runs
the rule's structured check across the WHOLE repo and measures real coverage,
then the numeric gate decides — exactly the gate the statistics path uses.

Three outcomes, matching the design discussion (inference is open, adoption is
gated by the repo's own code):

- verified  — check ran, coverage ≥ gate. A `naming` rule becomes a real
  facet rule the deterministic checker enforces (reproducible=True); other
  verified kinds carry measured coverage but stay LLM-judged at review until a
  matching review-side checker exists.
- rejected  — check ran, coverage below gate. Dropped with the number, e.g. the
  Ptr guess at 0%. This is the wrong-guess filter.
- unverified — no runnable check (kind="none"). Stays a facet=other guess,
  LLM-judged, reproducible=False (the pre-verification behaviour).

Today's check vocabulary is naming + header_directive: what runs without an AST.
It is a registry meant to grow — when inference keeps proposing a checkable kind
we cannot yet run, that names the next verifier to add (demand-driven, not
guessed up front). Structural checks (layering, ownership) need tree-sitter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from pumpkins.config import MIN_RULE_CONSISTENCY, MIN_RULE_OCCURRENCES
from pumpkins.conventions.extractor import casing_matches, select_files, split_pattern
from pumpkins.conventions.learner import ConventionRule
from pumpkins.conventions.proposer import (
    InferredRule,
    RuleCheck,
    _inferred_id,
    to_convention_rules,
)
from pumpkins.conventions.scope import RuleScope
from pumpkins.languages import cpp_parser

log = logging.getLogger(__name__)

# Headers a header_directive check inspects (TUs don't carry include guards).
_HEADER_EXTS = frozenset({".h", ".hpp", ".hh", ".hxx", ".inl"})
# A naming check for "member" spans both visibilities (and the pre-split alias).
_MEMBER_CATS = {"member", "member_variable", "private_member", "public_field"}


@dataclass
class CheckResult:
    matches: int
    total: int

    @property
    def coverage(self) -> float:
        return self.matches / self.total if self.total else 0.0


def _category_matches(want: str, observed: str) -> bool:
    if want in _MEMBER_CATS:
        return observed in {"private_member", "public_field", "member_variable"}
    return want == observed


def _naming_match(name: str, facet: str, value: str) -> bool:
    if facet == "prefix":
        return name.startswith(value)
    if facet == "suffix":
        return name.endswith(value)
    if facet == "casing":
        return casing_matches(split_pattern(name)[2], value)
    return False


def _first_code_line(text: str) -> str:
    """First non-blank, non-comment line — where an include guard would be."""
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("//") or s.startswith("/*") or s.startswith("*"):
            continue
        return s
    return ""


def verify(
    repo: Path,
    check: RuleCheck,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    include_tests: bool = False,
) -> CheckResult | None:
    """Measure a check across the repo, or None when it is not runnable.

    Uses the same file selection as the learn scan, so scoping / vendored-dir
    skipping / test exclusion behave identically."""
    files = select_files(repo, include, exclude, include_tests)

    if check.kind == "naming":
        if check.facet not in ("prefix", "suffix", "casing") or not check.value:
            return None
        matches = total = 0
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for observed, name in cpp_parser.scan(text):
                if _category_matches(check.category, observed):
                    total += 1
                    matches += _naming_match(name, check.facet, check.value)
        return CheckResult(matches, total)

    if check.kind == "header_directive":
        text_want = check.text.strip()
        if not text_want:
            return None
        headers = [p for p in files if p.suffix.lower() in _HEADER_EXTS]
        matches = 0
        for path in headers:
            try:
                first = _first_code_line(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            matches += first == text_want
        return CheckResult(matches, len(headers))

    return None


@dataclass
class VerificationReport:
    """The fate of a batch of inferred rules after measuring them."""

    verified: list[ConventionRule] = field(default_factory=list)
    unverified: list[ConventionRule] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (description, reason)


def _confidence(coverage: float) -> str:
    return "high" if coverage >= 0.95 else "medium"


def verify_inferred(
    repo: Path,
    inferred: list[InferredRule],
    scan_scope: RuleScope | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    include_tests: bool = False,
) -> VerificationReport:
    """Measure each inferred rule against the repo and sort into verified /
    rejected / unverified. Rejected guesses are dropped, not written."""
    scope = scan_scope or RuleScope()
    report = VerificationReport()
    taken: set[str] = set()

    for r in inferred:
        desc = r.rule.strip()
        if not desc:
            continue
        result = verify(repo, r.check, include, exclude, include_tests)

        if result is None:
            # No runnable check — stays an unverified, LLM-judged guess.
            report.unverified.extend(to_convention_rules([r], scope))
            continue

        if result.coverage < MIN_RULE_CONSISTENCY or result.matches < MIN_RULE_OCCURRENCES:
            report.rejected.append(
                (
                    desc,
                    f"측정 coverage {result.coverage:.0%} ({result.matches}/{result.total}) "
                    f"— 게이트({MIN_RULE_CONSISTENCY:.0%}/{MIN_RULE_OCCURRENCES}회) 미달",
                )
            )
            continue

        # Verified. A naming rule becomes a real facet rule the deterministic
        # checker enforces (reproducible at review); other kinds keep measured
        # coverage but stay facet=other until a review-side checker exists.
        if r.check.kind == "naming":
            category = "member_variable" if r.check.category in _MEMBER_CATS else r.check.category
            rule_id = _inferred_id(f"{category}-{r.check.facet}-{r.check.value}", taken)
            facet, value = r.check.facet, r.check.value
        else:
            category = r.kind or "layout"
            rule_id = _inferred_id(desc, taken)
            facet, value = "other", ""

        report.verified.append(
            ConventionRule(
                id=rule_id,
                category=category,
                description=desc,
                facet=facet,
                value=value,
                coverage=result.coverage,
                occurrences=result.matches,
                confidence=_confidence(result.coverage),
                examples=[r.evidence] if r.evidence else [],
                scope=scope.model_copy(deep=True),
            )
        )
    return report
