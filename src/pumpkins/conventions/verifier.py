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

Check vocabulary, by what it needs to run:

- naming, header_directive, include_direction — the regex parser. Layering is
  here on purpose: `#include` is lexically unambiguous, so the one structural
  rule most likely to matter keeps working where the native parser does not.
- return_type, member_ownership — a tree-sitter AST (languages/cpp/ast). If that
  native lib is broken/ABI-skewed, these degrade to "cannot verify" (None)
  rather than crashing or, worse, reporting a clean pass.

Every check's denominator is chosen to be the population the rule is *about*:
pointer-holding members for ownership, the files of one layer for layering. A
denominator wider than the rule quietly turns any claim into a true one — the
recurring failure this project has already paid for twice.

The registry is meant to grow: when inference keeps proposing a checkable kind
we cannot yet run, that names the next verifier to add (demand-driven).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pumpkins.config import MIN_RULE_CONSISTENCY, MIN_RULE_OCCURRENCES
from pumpkins.conventions.extractor import (
    casing_matches,
    collect_macros,
    select_files,
    split_pattern,
)
from pumpkins.conventions.learner import ConventionRule, RuleCheck
from pumpkins.conventions.proposer import (
    InferredRule,
    _inferred_id,
    to_convention_rules,
)
from pumpkins.conventions.scope import RuleScope
from pumpkins.languages.cpp import (
    ast as cpp_ast,
    parser as cpp_parser,
    query as cpp_query,
)

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

    if check.kind == "include_direction":
        return _verify_include_direction(repo, check, files)

    if check.kind == "member_ownership":
        return _verify_member_ownership(check, files, collect_macros(repo, files))

    if check.kind == "base_class":
        return _verify_base_class(check, files, collect_macros(repo, files))

    if check.kind == "query":
        return _verify_query(check, files, collect_macros(repo, files))

    if check.kind == "return_type":
        # Structural: needs the AST. Without tree-sitter we cannot measure it, so
        # the rule stays an unverified guess (None), the same safe degradation as
        # any other uncheckable rule — never a silent pass.
        want = check.type_contains.strip()
        if not want or not cpp_ast.require("structural rule verification"):
            return None
        prefix = check.name_prefix
        macros = collect_macros(repo, files)
        # Dedup by name: a function declared in a header and defined in a source
        # is one function, not two — counting both would inflate the occurrence
        # gate. Prefer an informative return type over a bare `auto`/empty one.
        seen: dict[str, str] = {}
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for fn in cpp_ast.functions(text, macros):
                if prefix and not fn.name.startswith(prefix):
                    continue
                prev = seen.get(fn.name)
                if prev is None or (prev in ("", "auto") and fn.return_type not in ("", "auto")):
                    seen[fn.name] = fn.return_type
        matches = sum(1 for rtype in seen.values() if want in rtype)
        return CheckResult(matches, len(seen))

    return None


def includes_of(text: str) -> list[str]:
    """Every `#include` target in a source, as written."""
    out = []
    for line in text.splitlines():
        m = cpp_parser.INCLUDE_RE.match(line)
        if m:
            out.append(m.group(1))
    return out


def _in_dir(rel_path: str, directory: str) -> bool:
    d = directory.strip("/")
    return bool(d) and (rel_path == d or rel_path.startswith(d + "/"))


def _verify_include_direction(repo: Path, check: RuleCheck, files: list[Path]) -> CheckResult | None:
    """Layering: how much of `from_dir` keeps clear of `forbidden_dir`.

    Denominator is the files under from_dir — not every file in the repo. A
    layering rule says something about one layer, so the rest of the repo has no
    business diluting it; measuring it repo-wide would let a large unrelated
    codebase push any direction rule over the gate for free.

    Include targets are resolved by file name against the repo's own files
    (`#include "widget.h"` rarely spells its repo-relative path), and headers
    that belong to nobody in the repo are external and simply not layering.
    """
    if not check.from_dir.strip() or not check.forbidden_dir.strip():
        return None

    rel = {p: p.relative_to(repo).as_posix() for p in files}
    forbidden_names = {
        p.name for p, r in rel.items() if _in_dir(r, check.forbidden_dir)
    }
    scoped = [p for p, r in rel.items() if _in_dir(r, check.from_dir)]
    if not scoped or not forbidden_names:
        # Either side empty means the rule is about directories this scan does
        # not contain — unmeasurable, not "perfectly followed".
        return None

    clean = 0
    for path in scoped:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        crosses = any(
            target.split("/")[-1] in forbidden_names for target in includes_of(text)
        )
        clean += not crosses
    return CheckResult(clean, len(scoped))


def _verify_member_ownership(
    check: RuleCheck, files: list[Path], macros: cpp_ast.MacroTable
) -> CheckResult | None:
    """Ownership: of the members that hold a pointer, how many hold it the stated way.

    The denominator is pointer-holding members only (ast.MemberDecl.holds_pointer).
    Counting every member would measure "what share of all fields are smart
    pointers", which is a different question with a much smaller answer — the
    denominator mistake this project keeps having to avoid.
    """
    want = check.value.strip().lower() or "smart"
    if want not in ("smart", "raw"):
        return None
    if not cpp_ast.require("ownership rule verification"):
        return None

    matches = total = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for member in cpp_ast.members(text, macros):
            if not member.holds_pointer:
                continue
            total += 1
            matches += member.is_smart_pointer if want == "smart" else member.is_raw_pointer
    return CheckResult(matches, total)


def _verify_base_class(
    check: RuleCheck, files: list[Path], macros: cpp_ast.MacroTable
) -> CheckResult | None:
    """Hierarchy: of the classes named like the rule says, how many derive as it says.

    Added because a real run asked for it: inference on yaml-cpp proposed "예외
    클래스는 std::runtime_error를 상속한다" and there was no check to run, so a
    true and useful rule sat in the unverified pile. The registry grows on
    demand — a kind we keep being asked for is the next one to build.

    The denominator is classes matching name_suffix, deduped by name: a class
    declared in a header and referenced elsewhere is one class.
    """
    suffix, want = check.name_suffix.strip(), check.base_contains.strip()
    if not suffix or not want:
        return None
    if not cpp_ast.require("hierarchy rule verification"):
        return None

    seen: dict[str, list[str]] = {}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for cls in cpp_ast.classes(text, macros):
            if not cls.name.endswith(suffix):
                continue
            # A forward declaration has no bases; a later definition does. Keep
            # whichever actually says something about the hierarchy.
            if cls.bases or cls.name not in seen:
                seen[cls.name] = cls.bases
    matches = sum(1 for bases in seen.values() if any(want in b for b in bases))
    return CheckResult(matches, len(seen))


def _verify_query(
    check: RuleCheck, files: list[Path], macros: cpp_ast.MacroTable
) -> CheckResult | None:
    """Measure a model-authored pair of queries across the repo.

    This is the branch that ends the enumeration: any convention the model can
    express as "these sites, and this is what they should look like" becomes
    measurable without new code here.

    The denominator comes from `population_query`, so it is the rule's own claim
    about what it governs rather than something this function decided. An empty
    population means unmeasurable (None), never "perfectly followed" — the same
    distinction every other check makes.
    """
    population = cpp_query.compile_query(check.population_query)
    conforming = cpp_query.compile_query(check.conforming_query)
    if population is None or conforming is None:
        return None
    if not cpp_ast.require("query rule verification"):
        return None

    matches = total = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tree = cpp_ast.parse_tree(text, macros)
        if tree is None:
            return None
        # One tree for both queries: spans are only comparable within a parse.
        in_population = population.subjects(tree)
        if not in_population:
            continue
        satisfied = conforming.subjects(tree)
        total += len(in_population)
        matches += len(in_population.keys() & satisfied.keys())
    return CheckResult(matches, total) if total else None


@dataclass
class VerificationReport:
    """The fate of a batch of inferred rules after measuring them."""

    verified: list[ConventionRule] = field(default_factory=list)
    unverified: list[ConventionRule] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (description, reason)
    # Which check kinds the model actually reached for, and how many queries it
    # wrote that would not compile. Instrumentation, not decoration: the point of
    # the `query` kind is that the model stops being limited to a fixed list, and
    # a capability nobody measures is a capability nobody knows is unused.
    kinds: Counter = field(default_factory=Counter)
    bad_queries: list[str] = field(default_factory=list)

    def kind_summary(self) -> str:
        return ", ".join(f"{k}×{n}" for k, n in self.kinds.most_common()) or "없음"


def _confidence(coverage: float) -> str:
    return "high" if coverage >= 0.95 else "medium"


# A check whose population is substantial but whose conforming side matches
# NOTHING is almost never a repo that violates its own convention everywhere —
# it is a check that failed to express the condition. Below this population size
# 0% could be a genuine tiny sample, so the distinction only applies above it.
MIN_POPULATION_FOR_BROKEN_CHECK = 10


def _rejection_reason(result: CheckResult, kind: str = "") -> str:
    """Why a guess did not become a rule — the failures mean different things.

    Three, and a human does something different for each:

    - the repo contradicts it        → drop the idea
    - too few instances to tell      → the rule may be right; write it by hand
    - the check matched nothing at all → the *check* is wrong, not the rule

    The third one matters most for model-authored queries. Measured live: the
    model wrote a population query that correctly found all 52 members, and a
    conforming query that matched none of them. Reporting that as "coverage 0% —
    the repo does not back this" blames the repo for a broken query and hides the
    one thing worth fixing.
    """
    # Only for `query`, where the conforming side is free-form and model-authored.
    # For the built-in kinds the checking logic is ours and tested, so 0% really
    # does mean the repo contradicts the rule — "pointer members end in Ptr"
    # measured 0% against 20 `m_`-prefixed members, and that guess was simply wrong.
    if (
        kind == "query"
        and result.matches == 0
        and result.total >= MIN_POPULATION_FOR_BROKEN_CHECK
    ):
        return (
            f"대상 {result.total}개를 찾았는데 만족하는 것이 0개 "
            f"— 규칙이 틀렸다기보다 질의가 조건을 잘못 표현한 것으로 보입니다"
        )
    if result.coverage < MIN_RULE_CONSISTENCY:
        return (
            f"측정 coverage {result.coverage:.0%} ({result.matches}/{result.total}) "
            f"— 기준 {MIN_RULE_CONSISTENCY:.0%} 미달 (레포가 뒷받침하지 않음)"
        )
    return (
        f"coverage {result.coverage:.0%}는 높지만 사례가 {result.matches}개뿐 "
        f"— 기준 {MIN_RULE_OCCURRENCES}개 미달 (우연과 구분 불가)"
    )


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
        report.kinds[r.check.kind] += 1
        if r.check.kind == "query":
            for q in (r.check.population_query, r.check.conforming_query):
                if q.strip() and cpp_query.compile_query(q) is None:
                    report.bad_queries.append(q.strip()[:120])

        result = verify(repo, r.check, include, exclude, include_tests)

        if result is None:
            # No runnable check — stays an unverified, LLM-judged guess.
            report.unverified.extend(to_convention_rules([r], scope))
            continue

        if result.coverage < MIN_RULE_CONSISTENCY or result.matches < MIN_RULE_OCCURRENCES:
            report.rejected.append((desc, _rejection_reason(result, r.check.kind)))
            continue

        # Verified. A naming rule becomes a real facet rule the deterministic
        # checker enforces (reproducible at review); other kinds keep measured
        # coverage but stay facet=other until a review-side checker exists.
        if r.check.kind == "naming":
            category = "member_variable" if r.check.category in _MEMBER_CATS else r.check.category
            rule_id = _inferred_id(f"{category}-{r.check.facet}-{r.check.value}", taken)
            facet, value = r.check.facet, r.check.value
            check = RuleCheck()  # naming uses facet/value; no separate check needed
        else:
            category = r.kind or "layout"
            rule_id = _inferred_id(desc, taken)
            facet, value = "other", ""
            # Carry the check to disk so the review side can re-run it
            # deterministically (structural rules like return_type).
            check = r.check

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
                check=check,
            )
        )
    return report
