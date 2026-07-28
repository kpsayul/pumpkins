"""Convention learning, stage L2 — LLM judgment + threshold gate.

Receives the naming statistics from stage L1 (extractor.py) and asks the LLM to
judge which patterns are actual *rules* of the project. The LLM's job here is
deliberately narrow — structured stats in, structured rules out — which is why
the cheaper learn-tier model suffices (docs/convention-detection-design.md §4).
The review pipeline keeps the stronger model.

Two safeguards from the design doc:
- threshold gate (§3-(2)) is enforced *in code*, not just in the prompt — a
  rule below MIN_RULE_OCCURRENCES / MIN_RULE_CONSISTENCY is demoted to a
  rejected candidate no matter what the LLM says.
- the output is human-reviewable and committed to the target repo (§3-(1));
  people can edit, approve or reject rules. Persistence and the approval
  workflow live in store.py — this module only decides what to *propose*.

The provider (Claude / GPT) is selected by LLM_PROVIDER via llm/provider.py;
each SDK reads its own API key from the environment.
"""

from __future__ import annotations

import logging
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from pumpkins.config import (
    LEARN_TEMPERATURE,
    MIN_RULE_CONSISTENCY,
    MIN_RULE_OCCURRENCES,
    default_learn_model,
)
from pumpkins.conventions.extractor import CategoryStats, detect_split_signal
from pumpkins.conventions.scope import RuleScope
from pumpkins.llm.provider import get_client

log = logging.getLogger(__name__)

_SYSTEM_PROMPT_TEMPLATE = """\
You are analyzing identifier-naming statistics extracted from a C++ repository
to discover the project's *implicit* naming conventions — rules the team
follows in code even though they may be written down nowhere.

You receive, per identifier category (member_variable / constant / function /
class_type): a total count, distributions over prefixes, suffixes and casing
styles, and a sample of raw names. `constant` covers compile-time constants
(`static constexpr` / `static const`), which are counted apart from mutable
members because projects usually name the two differently.

Casing has a SMALLER denominator than prefix and suffix, stated explicitly in
the input. Single-word lowercase names (`dump`, `value`) are excluded from it:
they satisfy lowerCamel and lower_snake equally, so they carry no casing
signal. When you report occurrences and coverage for a casing rule, use that
casing denominator — never the category total.

Adopt a pattern as a rule ONLY when the statistics clearly support it:
- at least {min_occ} occurrences in the relevant denominator, AND
- the dominant pattern covers at least {min_cons:.0%} of it.
A category split between two competing patterns is NOT a rule — reject it and
say why. (Suggesting unification is a separate, out-of-scope feature.)

Also inspect the raw samples for conventions the canned statistics cannot
express (e.g. interface classes prefixed with `I`, factory functions starting
with `make`). Adopt such a rule only if the samples show it consistently, and
estimate its occurrences/coverage honestly from what you can see.

For every adopted rule provide:
- id: short kebab-case slug (e.g. `member-prefix-m_`)
- description: one sentence, in Korean, phrased so it can directly back a
  review comment (e.g. "멤버 변수는 `m_` 접두사를 사용한다")
- facet + value: the machine-checkable form used by the review pipeline to
  flag violations deterministically. facet is one of prefix / suffix / casing,
  and value must be copied EXACTLY from the statistics keys — e.g.
  (facet: prefix, value: m_), (facet: casing, value: lowerCamel),
  (facet: suffix, value: _). value "(none)" means the facet must be absent.
  Use facet: other (free-text value) only for sample-based rules that don't
  fit these facets — those are reported in the file but not auto-checked.
- coverage and occurrences taken honestly from the statistics
- confidence: high (coverage >= 95%), medium (>= 85%), otherwise do not adopt
- a few examples (conforming names) and counter_examples (violations) drawn
  from the samples

Leave `scope` empty — you are given statistics, not file paths, so you cannot
know where a rule should be narrowed. The caller fills it in.

Report rejected candidates briefly with a reason. Be conservative: every wrong
rule becomes a false review comment later, and false comments are what make
users turn the tool off.

Some categories are marked POSSIBLE HIDDEN SPLIT. That means the numbers are
close to two groups rather than scattered, which often happens when one
category actually holds two kinds of thing with two different conventions —
measured together they average into a number that passes no threshold. For each
such marker, look at the raw samples and report a `split_hypotheses` entry:

- `category` and `facet` copied exactly from the marker, nothing appended
- what distinguishes the two groups, if you can tell. The directories listed
  under each group are the most common answer: code from a bundled third-party
  library, generated output, or test scaffolding follows its own conventions and
  is not this project stating a rule. Other answers: interface types vs
  implementations, static vs instance, public API vs internals.
- `checkable: true` only when the distinguishing property is visible in the
  declaration itself or in the file path, so a mechanical check could use it.
  `false` when telling the groups apart needs understanding what the code means.
- if the numbers look like genuine inconsistency rather than two groups, say so
  and leave `discriminator` empty. Do not invent a boundary to explain noise.

A split hypothesis is a question for a human, not a rule. It is never enforced.
"""


class ConventionRule(BaseModel):
    """One adopted naming rule — persisted as one file under the repo's pumpkins/.

    facet/value make the rule machine-checkable: the review pipeline compares
    an identifier's split_pattern() facets against them deterministically
    (conventions/checker.py). facet="other" rules are human-readable only.

    `scope` narrows where the rule is enforced. The LLM never fills it in — it
    sees statistics, not paths — so it arrives either from the learn scan's own
    scope or from a human editing the file.
    """

    id: str
    category: str
    description: str
    facet: Literal["prefix", "suffix", "casing", "other"] = "other"
    value: str = ""
    coverage: float = Field(ge=0.0, le=1.0)
    occurrences: int
    confidence: Literal["high", "medium", "low"]
    examples: list[str] = Field(default_factory=list)
    counter_examples: list[str] = Field(default_factory=list)
    scope: RuleScope = Field(default_factory=RuleScope)


class RejectedCandidate(BaseModel):
    """A pattern considered but not adopted — kept in the file for transparency."""

    category: str
    pattern: str
    reason: str


class SplitHypothesis(BaseModel):
    """A guess at why a category failed the threshold: two groups, not noise.

    Informational only — written to the repo's pumpkins/learn-report.yml for a human to read.
    Acting on it means either splitting the category in the extractor (when
    `checkable`) or writing a scoped rule by hand, both human decisions.
    """

    category: str
    facet: str
    groups: str            # 관측된 두 무리
    discriminator: str = ""  # 무엇이 둘을 가르는가 (모르면 빈 문자열)
    checkable: bool = False  # 선언이나 경로에서 기계적으로 확인 가능한가
    note: str = ""


class LearnResult(BaseModel):
    rules: list[ConventionRule]
    rejected: list[RejectedCandidate] = Field(default_factory=list)
    split_hypotheses: list[SplitHypothesis] = Field(default_factory=list)


def apply_threshold_gate(result: LearnResult) -> LearnResult:
    """Code-side enforcement of the numeric rule definition (design doc §3-(2)).

    The prompt states the thresholds too, but we never rely on the LLM to obey
    them — determinism here is what makes conventions.yml trustworthy.
    """
    kept: list[ConventionRule] = []
    rejected = list(result.rejected)
    for rule in result.rules:
        if rule.occurrences < MIN_RULE_OCCURRENCES or rule.coverage < MIN_RULE_CONSISTENCY:
            rejected.append(
                RejectedCandidate(
                    category=rule.category,
                    pattern=rule.id,
                    reason=(
                        f"threshold gate: occurrences={rule.occurrences} "
                        f"(min {MIN_RULE_OCCURRENCES}), coverage={rule.coverage:.0%} "
                        f"(min {MIN_RULE_CONSISTENCY:.0%})"
                    ),
                )
            )
        else:
            kept.append(rule)
    return LearnResult(
        rules=kept, rejected=rejected, split_hypotheses=result.split_hypotheses
    )


class ConventionLearner:
    def __init__(self, model: str | None = None):
        self.model = model or default_learn_model()
        self.client = get_client()  # provider from LLM_PROVIDER; key from env

    def learn(
        self, stats: list[CategoryStats], scan_scope: RuleScope | None = None
    ) -> LearnResult:
        parsed = self.client.parse(
            model=self.model,
            max_tokens=8000,
            system=_SYSTEM_PROMPT_TEMPLATE.format(
                min_occ=MIN_RULE_OCCURRENCES, min_cons=MIN_RULE_CONSISTENCY
            ),
            user=_render_stats_text(stats),
            schema=LearnResult,
            # Passed explicitly (None → provider default) so the difference from
            # the review stage is a recorded decision, not an oversight.
            temperature=LEARN_TEMPERATURE,
        )
        result = parsed.parsed
        if result is None:
            raise RuntimeError("LLM returned no parseable convention output")
        log.info(
            "LLM proposed %d rule(s), %d rejection(s), %d split hypothesis(es); "
            "tokens in=%d out=%d",
            len(result.rules),
            len(result.rejected),
            len(result.split_hypotheses),
            parsed.input_tokens,
            parsed.output_tokens,
        )
        # Rules can only be trusted where they were measured: a scan narrowed to
        # a subtree yields rules scoped to that subtree. Assigned in code, never
        # taken from the model, for the same reason as the threshold gate.
        scope = scan_scope or RuleScope()
        for rule in result.rules:
            rule.scope = scope.model_copy(deep=True)
        return apply_threshold_gate(result)


# ----------------------------------------------------------------- rendering

def _split_marker(stats: CategoryStats, facet: str, counts: dict[str, int], total: int) -> str:
    signal = detect_split_signal(counts, total, MIN_RULE_CONSISTENCY)
    if signal is None:
        return ""
    (a, na), (b, nb) = signal
    # The names matter more than the percentages here: "63% vs 36%" says
    # nothing, while `MatchAndExplain, DescribeTo` next to `format_to, vformat`
    # says one group is a bundled test framework.
    def group(value: str, count: int) -> str:
        names = ", ".join(stats.facet_samples.get(f"{facet}={value}", [])) or "(none)"
        dirs = ", ".join(stats.facet_dirs.get(f"{facet}={value}", [])) or "(unknown)"
        return (
            f"    value={value} ({count}, {count / total:.0%})\n"
            f"      names: {names}\n"
            f"      from:  {dirs}"
        )

    return (
        f"POSSIBLE HIDDEN SPLIT in category={stats.category} facet={facet} "
        f"— two groups, not scatter:\n"
        f"{group(a, na)}\n{group(b, nb)}\n"
        f"    What separates them? The directories are often the answer."
    )


def _render_stats_text(stats: list[CategoryStats]) -> str:
    parts = ["## Identifier statistics\n"]
    for s in stats:
        parts.append(f"### {s.category} (total {s.total})")
        parts.append(f"prefixes: {_fmt_counts(s.prefix_counts, s.total)}")
        parts.append(f"suffixes: {_fmt_counts(s.suffix_counts, s.total)}")
        parts.append(
            f"casing (denominator {s.casing_informative}; "
            f"{s.casing_ambiguous} single-word name(s) excluded as unsignalled): "
            f"{_fmt_counts(s.casing_counts, s.casing_informative)}"
        )
        parts.append(f"samples:  {', '.join(s.samples) or '(none)'}")
        for facet, counts, total in (
            ("prefix", s.prefix_counts, s.total),
            ("suffix", s.suffix_counts, s.total),
            ("casing", s.casing_counts, s.casing_informative),
        ):
            marker = _split_marker(s, facet, counts, total)
            if marker:
                parts.append(marker)
        parts.append("")
    return "\n".join(parts)


def _fmt_counts(counts: dict[str, int], total: int) -> str:
    if not counts or total <= 0:
        return "(none)"
    return ", ".join(
        f"{k}: {v} ({v / total:.0%})" for k, v in counts.items()
    )


def render_stats_yaml(stats: list[CategoryStats]) -> str:
    """Debug dump for `pumpkins learn --no-llm` — the exact stats the LLM would see."""
    return yaml.safe_dump(
        {"identifier_stats": [s.model_dump() for s in stats]},
        allow_unicode=True,
        sort_keys=False,
    )
