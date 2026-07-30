"""Convention checking — compare a diff against conventions.yml (no LLM).

This is the review-side half of the convention axis
(docs/convention-detection-design.md §6 MVP 2단계): identifiers declared on
the *added* lines of the diff are matched against the machine-checkable rules
(facet/value) that `pumpkins learn` adopted. Violations become question-form
findings — the tool asks, it doesn't accuse (§3-(3)).

Deliberately deterministic: same diff + same conventions.yml → same findings,
so this stage runs even without an API key and is CI-safe. The planned LLM
assist (design doc 방안 B — judging context the rules can't express, proposing
new rule candidates) layers on top later; facet="other" rules are skipped here
until then.

Each rule is applied only to the files its `scope` covers (conventions/scope.py),
which is how a repo keeps different conventions in different subtrees.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from unidiff import PatchSet

from pumpkins.config import (
    DETERMINISTIC_STRUCTURAL_AST,
    DETERMINISTIC_STRUCTURAL_CHECKS,
)
from pumpkins.conventions.extractor import (
    casing_matches,
    match_identifiers,
    sanitize_line,
    split_pattern,
)
from pumpkins.conventions.learner import ConventionRule
from pumpkins.conventions.store import load_active_rules
from pumpkins.languages.cpp import (
    ast as cpp_ast,
    parser as cpp_parser,
    query as cpp_query,
)
from pumpkins.models import DetectorKind, DiffScope, Evidence, Finding, Severity

log = logging.getLogger(__name__)

# A hunk is treated as class-body context (enabling member-variable matching)
# only if it visibly contains class scaffolding — otherwise a `int count;`
# added line is more likely a local variable.
_MEMBER_CONTEXT_RE = re.compile(
    r"^\s*(?:public|private|protected)\s*:|\b(?:class|struct)\s+[A-Za-z_]"
)

# A rule written before members were split by visibility still has to work: the
# generic category matches either side. The reverse is deliberately not true —
# a `private_member` rule never applies to a member whose visibility we could
# not see, because guessing is how false positives get made.
_CATEGORY_ALIASES = {"member_variable": ("private_member", "public_field")}


def _category_matches(rule_category: str, observed: str) -> bool:
    return observed == rule_category or observed in _CATEGORY_ALIASES.get(
        rule_category, ()
    )


def _opening_access(hunk) -> str | None:
    """Visibility in effect at the START of a hunk, or None when not visible.

    A hunk shows only a window. Git's section header carries the enclosing
    context — often the specifier itself (`@@ … @@ private:`) or the class line,
    whose keyword decides the default. If neither is there the visibility is
    unknown and members stay in the generic category rather than being guessed.

    Only the *opening* state comes from here. Specifiers inside the hunk move it
    line by line (see check_scope): resolving one visibility for a whole hunk
    put every member after a second specifier in the wrong bucket — a hunk
    spanning `public:` … `private:` marked the private ones public.
    """
    header = hunk.section_header or ""
    m = cpp_parser.ACCESS_RE.match(header)
    if m:
        return cpp_parser.normalize_access(m.group(1))
    if re.search(r"\b(class|struct)\s+[A-Za-z_]", header):
        return cpp_parser.default_access(header)
    return None

_PREFIX_STRIP = {"m_": 2, "s_": 2, "g_": 2, "m": 1, "k": 1, "s": 1, "g": 1}


def load_conventions(path: Path) -> list[ConventionRule]:
    """Load the rules the review should enforce.

    `path` is either a `pumpkins/` store (only `rules/` is enforced — pending
    candidates deliberately have no effect) or a legacy single `conventions.yml`.
    """
    rules = load_active_rules(path)
    skipped = sum(1 for r in rules if r.facet == "other")
    if skipped:
        log.debug("%d rule(s) with facet=other are not auto-checkable — skipped", skipped)
    return rules


def check_scope(scope: DiffScope, rules: list[ConventionRule]) -> list[Finding]:
    """Match identifiers declared on added lines against the adopted rules."""
    checkable = [r for r in rules if r.facet in ("prefix", "suffix", "casing")]
    if not checkable:
        return []

    findings: list[Finding] = []
    seen: set[tuple[str, str, str]] = set()  # (file, rule id, name) — report once

    for file_diff in scope.files:
        # A rule only judges the files its scope covers, so legacy or generated
        # subtrees can keep their own conventions instead of generating noise.
        in_scope = [r for r in checkable if r.scope.applies_to(file_diff.path)]
        if not in_scope:
            log.debug("no in-scope convention rules for %s", file_diff.path)
            continue

        try:
            patched = PatchSet(file_diff.patch_text)[0]
        except Exception as exc:
            log.debug("could not re-parse patch for %s: %s", file_diff.path, exc)
            continue

        for hunk in patched:
            # Class scaffolding may be visible in the hunk lines, or — for
            # additions deep inside a class body — only in git's hunk section
            # header (`@@ ... @@ class ThreadPool {` / `private:`).
            member_context = bool(
                _MEMBER_CONTEXT_RE.search(hunk.section_header or "")
            ) or any(_MEMBER_CONTEXT_RE.search(l.value) for l in hunk)
            access = _opening_access(hunk) if member_context else None
            for line in hunk:
                # Context lines count too: an unchanged `private:` above an
                # added member is exactly how a diff shows the boundary.
                specifier = cpp_parser.ACCESS_RE.match(line.value)
                if specifier:
                    access = cpp_parser.normalize_access(specifier.group(1))
                    continue
                if not line.is_added or line.target_line_no is None:
                    continue
                sanitized = sanitize_line(line.value.rstrip("\n"))
                for category, name in match_identifiers(sanitized, member_context, access):
                    facets = dict(
                        zip(("prefix", "suffix", "casing"), split_pattern(name))
                    )
                    for rule in in_scope:
                        if not _category_matches(rule.category, category):
                            continue
                        if _satisfies(facets[rule.facet], rule):
                            continue
                        key = (file_diff.path, rule.id, name)
                        if key in seen:
                            continue
                        seen.add(key)
                        findings.append(
                            _violation_finding(
                                file_diff.path, line.target_line_no, name, rule
                            )
                        )

    log.info("convention check: %d finding(s) from %d rule(s)", len(findings), len(checkable))
    return findings


# ------------------------------------------------------------------ internals

def _satisfies(observed: str, rule: ConventionRule) -> bool:
    """Whether an identifier's facet value satisfies the rule.

    Casing is compared leniently: a single-word lowercase name carries no
    casing signal, so `flush` must not be reported as breaking a lowerCamel
    rule (see extractor.AMBIGUOUS_CASING).
    """
    if rule.facet == "casing":
        return casing_matches(observed, rule.value)
    return observed == rule.value


def _violation_finding(path: str, line_no: int, name: str, rule: ConventionRule) -> Finding:
    """Build a question-form finding (design doc §3-(3): ask, don't accuse)."""
    reach = "" if rule.scope.is_repo_wide else f", 적용 범위: {rule.scope.describe()}"
    explanation = (
        f"이 리포의 {rule.category} {rule.occurrences}개 중 {rule.coverage:.0%}가 "
        f"이 관행을 따릅니다 (근거: conventions.yml `{rule.id}`{reach}). "
        f"여기만 다르게 한 이유가 있을까요? 의도한 예외라면 무시하셔도 됩니다."
    )
    return Finding(
        file=path,
        line=line_no,
        # Question-form, low-stakes by design — naming never outranks a bug.
        severity=Severity.low,
        title=f"`{name}` — {rule.description} 관행과 다른 것 같아요",
        explanation=explanation,
        suggestion=_suggest_rename(name, rule),
        evidence=Evidence(
            detector=DetectorKind.convention,
            rule_id=rule.id,
            # The same numbers the explanation states in prose, kept structured
            # so runs and rules can be compared without parsing Korean.
            occurrences=rule.occurrences,
            coverage=rule.coverage,
            rule_scope=rule.scope.describe(),
        ),
    )


def _suggest_rename(name: str, rule: ConventionRule) -> str:
    """Mechanical rename proposal for prefix/suffix rules (casing is left to
    the reviewer — safe automatic case conversion needs word boundaries)."""
    prefix, suffix, _ = split_pattern(name)
    if rule.facet == "prefix":
        core = name[_PREFIX_STRIP[prefix]:] if prefix in _PREFIX_STRIP else name.lstrip("_")
        renamed = core if rule.value == "(none)" else f"{rule.value}{core}"
    elif rule.facet == "suffix":
        core = name.rstrip("_")
        renamed = core if rule.value == "(none)" else f"{core}{rule.value}"
    else:
        return ""
    if renamed == name:
        return ""
    return f"관행에 맞추면 `{renamed}` 이 됩니다."


# ------------------------------------------------ structural (AST) checking

# Which check kinds run here, and which of those need the native parser. Both
# come from config so the LLM prompt can exclude exactly what this enforces —
# see config.DETERMINISTIC_STRUCTURAL_CHECKS for why they must not drift.


def _line_in_ranges(line: int, ranges) -> bool:
    return any(r.start <= line <= r.end for r in ranges)


def check_structural(scope: DiffScope, rules: list[ConventionRule], repo: Path) -> list[Finding]:
    """Deterministically flag diff violations of verified STRUCTURAL rules.

    A naming rule matches identifiers on added lines (check_scope); a structural
    rule asks about shape — what a function returns, how a member holds what it
    points at, which layer a file is allowed to include. Each is checked against
    what the diff actually touched, never against the whole file, so an untouched
    pre-existing violation is not reported as new work.

    Same detector/severity as check_scope: structural findings are reproducible
    and CI-safe. When the native parser is missing, only the AST-backed kinds
    drop out — the layering check still runs, and the LLM stage still sees every
    rule regardless.
    """
    structural = [
        r for r in rules
        if getattr(r, "check", None) is not None and r.check.kind in DETERMINISTIC_STRUCTURAL_CHECKS
    ]
    if not structural:
        return []
    if not cpp_ast.require("structural review check"):
        structural = [r for r in structural if r.check.kind not in DETERMINISTIC_STRUCTURAL_AST]
        if not structural:
            return []

    findings: list[Finding] = []
    seen: set[tuple[str, str, str]] = set()  # (file, rule id, subject) — report once
    layer_index = _ForbiddenHeaderIndex(repo)
    # The repo's own macro names, so a class header reads here exactly as it did
    # when the rule was learned. Two different readings of the same file would
    # mean a rule measured at 100% could still fire on conforming code.
    macros = _repo_macros(repo) if any(
        r.check.kind in DETERMINISTIC_STRUCTURAL_AST for r in structural
    ) else cpp_ast.NO_MACROS

    for file_diff in scope.files:
        in_scope = [r for r in structural if r.scope.applies_to(file_diff.path)]
        if not in_scope:
            continue

        needs_source = any(r.check.kind in DETERMINISTIC_STRUCTURAL_AST for r in in_scope)
        text = ""
        if needs_source:
            try:
                text = (repo / file_diff.path).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                log.debug("structural check: cannot read %s: %s", file_diff.path, exc)

        # Only what the diff touched (declared on a changed line).
        touched_fns = [
            f for f in cpp_ast.functions(text, macros)
            if _line_in_ranges(f.line, file_diff.added_ranges)
        ] if text else []
        touched_members = [
            m for m in cpp_ast.members(text, macros)
            if _line_in_ranges(m.line, file_diff.added_ranges)
        ] if text else []
        touched_classes = [
            c for c in cpp_ast.classes(text, macros)
            if _line_in_ranges(c.line, file_diff.added_ranges)
        ] if text else []

        # A query check needs the parse tree itself, not extracted declarations.
        tree = (
            cpp_ast.parse_tree(text, macros)
            if text and any(r.check.kind == "query" for r in in_scope)
            else None
        )

        for rule in in_scope:
            for subject, finding in _violations(
                file_diff, rule, touched_fns, touched_members, touched_classes,
                layer_index, tree,
            ):
                key = (file_diff.path, rule.id, subject)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(finding)

    log.info("structural check: %d rule(s) → %d finding(s)", len(structural), len(findings))
    return findings


def _repo_macros(repo: Path):
    """Object-like macro names the repo defines. Cached per process.

    Collected lazily and only when an AST-backed structural rule is active, so a
    review with no such rule never pays for the walk."""
    cached = _MACRO_CACHE.get(repo)
    if cached is None:
        from pumpkins.conventions.extractor import collect_macros

        cached = collect_macros(repo)
        _MACRO_CACHE[repo] = cached
    return cached


_MACRO_CACHE: dict[Path, object] = {}


def _violations(file_diff, rule, functions, members, classes, layer_index, tree=None):
    """Yield (subject, finding) for one rule against one changed file."""
    kind = rule.check.kind
    if kind == "return_type":
        for fn in functions:
            if _return_type_violates(fn, rule):
                yield fn.name, _structural_finding(
                    file_diff.path, fn.line, fn.name,
                    f"`{fn.name}`의 반환 타입 `{fn.return_type}`", rule,
                )
    elif kind == "member_ownership":
        want_smart = (rule.check.value or "smart").lower() == "smart"
        for member in members:
            if not member.holds_pointer:
                continue
            conforms = member.is_smart_pointer if want_smart else member.is_raw_pointer
            if not conforms:
                yield member.name, _structural_finding(
                    file_diff.path, member.line, member.name,
                    f"`{member.owner}::{member.name}`의 타입 `{member.type_text}`", rule,
                )
    elif kind == "base_class":
        suffix, want = rule.check.name_suffix, rule.check.base_contains
        for cls in classes:
            if not cls.name.endswith(suffix):
                continue
            if not any(want in b for b in cls.bases):
                derives = ", ".join(cls.bases) if cls.bases else "(상속 없음)"
                yield cls.name, _structural_finding(
                    file_diff.path, cls.line, cls.name,
                    f"`{cls.name}`가 상속하는 것은 `{derives}`", rule,
                )
    elif kind == "query":
        # Sites the rule governs but that do not satisfy it — and only the ones
        # the diff touched, so adopting a rule late does not indict old code.
        if tree is None:
            return
        population = cpp_query.compile_query(rule.check.population_query)
        conforming = cpp_query.compile_query(rule.check.conforming_query)
        if population is None or conforming is None:
            return
        satisfied = conforming.subjects(tree)
        for span, subject in population.subjects(tree).items():
            if span in satisfied:
                continue
            if not _line_in_ranges(subject.line, file_diff.added_ranges):
                continue
            yield subject.text, _structural_finding(
                file_diff.path, subject.line, subject.text,
                f"`{subject.text}`", rule,
            )
    elif kind == "include_direction":
        for line_no, target in _added_includes(file_diff):
            if layer_index.belongs_to(target, rule.check.forbidden_dir):
                yield target, _structural_finding(
                    file_diff.path, line_no, target,
                    f"새로 추가된 `#include \"{target}\"`", rule,
                )


def _added_includes(file_diff) -> list[tuple[int, str]]:
    """(line number, include target) for `#include`s the diff ADDS.

    Added lines only: a layering rule adopted after the fact will find existing
    crossings, and reporting those turns every unrelated edit to the file into a
    wall of complaints about code the author did not write.
    """
    try:
        patched = PatchSet(file_diff.patch_text)[0]
    except Exception as exc:
        log.debug("could not re-parse patch for %s: %s", file_diff.path, exc)
        return []
    out = []
    for hunk in patched:
        for line in hunk:
            if not line.is_added or line.target_line_no is None:
                continue
            m = cpp_parser.INCLUDE_RE.match(line.value)
            if m:
                out.append((line.target_line_no, m.group(1)))
    return out


class _ForbiddenHeaderIndex:
    """Which repo headers live under a given directory, resolved by file name.

    An `#include` is written relative to whatever is on the include path, so its
    text usually is not the repo-relative path. Names are indexed lazily and
    cached per directory: a review touching no layering rule pays nothing.
    """

    def __init__(self, repo: Path):
        self.repo = repo
        self._cache: dict[str, set[str]] = {}

    def _names_under(self, directory: str) -> set[str]:
        d = directory.strip("/")
        if d not in self._cache:
            root = self.repo / d
            self._cache[d] = (
                {p.name for p in root.rglob("*") if p.is_file()} if root.is_dir() else set()
            )
            if not self._cache[d]:
                log.debug("layering check: no files under %s", directory)
        return self._cache[d]

    def belongs_to(self, include_target: str, directory: str) -> bool:
        if not directory.strip():
            return False
        if include_target.strip("/").startswith(directory.strip("/") + "/"):
            return True
        return include_target.split("/")[-1] in self._names_under(directory)


def _return_type_violates(fn, rule: ConventionRule) -> bool:
    """A function matching the rule's name prefix whose return type breaks it."""
    check = rule.check
    if not fn.name.startswith(check.name_prefix):
        return False
    return check.type_contains not in fn.return_type


def _structural_finding(
    path: str, line: int, subject: str, observed: str, rule: ConventionRule
) -> Finding:
    reach = "" if rule.scope.is_repo_wide else f", 적용 범위: {rule.scope.describe()}"
    explanation = (
        f"이 리포의 관행({rule.description}) — {rule.occurrences}개 중 {rule.coverage:.0%}가 "
        f"따릅니다 (근거: `{rule.id}`{reach}). {observed}은 이와 다른 것 같아요. "
        f"의도한 예외라면 무시하셔도 됩니다."
    )
    return Finding(
        file=path,
        line=line,
        severity=Severity.low,  # a convention question never outranks a bug
        title=f"`{subject}` — {rule.description} 관행과 다른 것 같아요",
        explanation=explanation,
        evidence=Evidence(
            detector=DetectorKind.convention,  # deterministic → reproducible
            rule_id=rule.id,
            occurrences=rule.occurrences,
            coverage=rule.coverage,
            rule_scope=rule.scope.describe(),
        ),
    )
