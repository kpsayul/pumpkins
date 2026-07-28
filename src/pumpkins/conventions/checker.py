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

from pumpkins.conventions.extractor import (
    casing_matches,
    match_identifiers,
    sanitize_line,
    split_pattern,
)
from pumpkins.conventions.learner import ConventionRule
from pumpkins.conventions.store import load_active_rules
from pumpkins.languages import cpp
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


def _hunk_access(hunk) -> str | None:
    """Visibility in effect for a diff hunk, or None when it is not visible.

    A hunk shows only a window. If no specifier appears in it or in git's
    section header, the visibility is unknown and members are left in the
    generic category rather than assumed.
    """
    for text in [hunk.section_header or ""] + [l.value for l in hunk]:
        m = cpp.ACCESS_RE.match(text)
        if m:
            return cpp.normalize_access(m.group(1))
    for text in [hunk.section_header or ""] + [l.value for l in hunk]:
        if re.search(r"\b(class|struct)\s+[A-Za-z_]", text):
            return cpp.default_access(text)
    return None

_PREFIX_STRIP = {"m_": 2, "s_": 2, "g_": 2, "m": 1, "k": 1, "s": 1, "g": 1}


def load_conventions(path: Path) -> list[ConventionRule]:
    """Load the rules the review should enforce.

    `path` is either a `conventions/` store (only `rules/` is enforced — pending
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
            access = _hunk_access(hunk) if member_context else None
            for line in hunk:
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
