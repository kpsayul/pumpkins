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
from dataclasses import dataclass
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from pumpkins.config import (
    LEARN_TEMPERATURE,
    MIN_RULE_CONSISTENCY,
    MIN_RULE_OCCURRENCES,
    default_learn_model,
    default_reasoning_model,
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

Do NOT try to explain why a split category fails or where its boundary lies —
that reasoning is done in a separate, stronger-model step. Just reject a split
distribution as "not a rule" and move on.
"""


# Stage 2 — split adjudication, run on the strong (reasoning) tier only when a
# hidden split was detected in code. Kept apart from the stage-1 prompt because
# the two tasks have different natures (design doc §4.1): stage 1 is structured
# classification the cheap tier handles; naming a split's boundary is inference
# the cheap tier provably fails — measured on fmt, it read the directories and
# still said "no structural distinction". So only this task escalates, and only
# the split groups (not the whole stats) are sent, keeping the escalation cheap.
_SPLIT_SYSTEM_PROMPT = """\
You are told about identifier categories in a C++ repository whose naming
distribution looks like TWO groups rather than one convention — close to
bimodal, not scattered. This usually means one category holds two kinds of thing
with two different conventions (e.g. a bundled test framework vs the project's
own code, interface types vs implementations, public API vs internals), so
measured together they average into a number that passes no threshold.

Your only job is to name what separates the two groups, so a human can decide
whether to split the category or write a scoped rule. For each POSSIBLE HIDDEN
SPLIT block report one `split_hypotheses` entry:

- `category` and `facet`: copied EXACTLY from the block header, nothing appended.
- `groups`: a short description of the two observed groups (values + the sample
  names/directories you were shown).
- `discriminator`: what distinguishes them, if you can tell. The directories
  listed under each group are the most common answer — code from a bundled
  third-party library, generated output or test scaffolding follows its own
  conventions and is not this project stating a rule. Leave it empty if the
  numbers look like genuine inconsistency rather than two groups; do not invent
  a boundary to explain noise.
- `checkable`: true ONLY when the distinguishing property is visible in the
  declaration itself or in the file path, so a mechanical check could use it.
  false when telling the groups apart needs understanding what the code means.
- `note`: optional, one line of extra context.

A split hypothesis is a question for a human, not a rule. It is never enforced.
"""


class RuleCheck(BaseModel):
    """A machine-executable check attached to a rule.

    Lives here (with the rule schema) rather than in proposer.py so it can be
    persisted on a ConventionRule without a circular import: the inference path
    fills it, the review path re-runs it. kind="none" means the rule is not
    mechanically checkable and stays LLM-judged.

    naming/header_directive/include_direction run on the regex parser;
    return_type and member_ownership are structural and run on a tree-sitter AST
    (conventions/verifier.py, languages/cpp/ast.py).

    Layering deliberately does NOT need the AST: an include line is lexically
    unambiguous, and "which layer may depend on which" is too load-bearing a
    rule to lose on a machine where the native parser failed to build.
    """

    kind: Literal[
        "naming",
        "header_directive",
        "return_type",
        "include_direction",
        "member_ownership",
        "base_class",
        "none",
    ] = "none"
    # naming
    category: str = ""   # member | function | class_type | constant
    facet: Literal["prefix", "suffix", "casing", ""] = ""
    # naming: the affix/casing. member_ownership: "smart" | "raw".
    value: str = ""
    # header_directive
    text: str = ""
    # return_type (structural, needs tree-sitter)
    name_prefix: str = ""     # only functions whose name starts with this
    type_contains: str = ""   # ...must return a type whose text contains this
    # include_direction (layering): files under from_dir must not include
    # anything belonging to forbidden_dir.
    from_dir: str = ""
    forbidden_dir: str = ""
    # base_class (hierarchy): classes whose name ends in name_suffix derive from
    # a base whose name contains base_contains.
    name_suffix: str = ""
    base_contains: str = ""


class ConventionRule(BaseModel):
    """One adopted rule — persisted as one file under the repo's pumpkins/.

    facet/value make a naming rule machine-checkable: the review pipeline
    compares an identifier's split_pattern() facets against them deterministically
    (conventions/checker.py). A `facet="other"` rule is either LLM-judged, or —
    when it carries a structural `check` (e.g. return_type) — checked
    deterministically against the diff's AST by the same checker.

    `scope` narrows where the rule is enforced. The LLM never fills scope or
    check in on the statistics path — it sees statistics, not paths — so they
    arrive from the learn scan's scope, the inference/verify step, or a human.
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
    # A structural check (e.g. return_type) carried to disk so review can re-run
    # it deterministically. Empty (kind="none") for naming rules, which use
    # facet/value instead, and for LLM-judged rules.
    check: RuleCheck = Field(default_factory=RuleCheck)


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


class SplitAdjudication(BaseModel):
    """Stage-2 output: the strong model's read of the detected hidden splits."""

    split_hypotheses: list[SplitHypothesis] = Field(default_factory=list)


@dataclass
class LearnOutcome:
    """learn 한 번의 결과와 그 호출의 토큰 사용량.

    `learn()`은 규칙만 돌려주지만(하위호환), 검증 하네스처럼 호출 비용을 기록해야 하는
    쪽은 사용량이 필요하다. 예전에는 usage가 로그로만 남아 하네스가 클라이언트를 직접
    호출해야 했다 — `learn_with_usage()`가 이를 공개 API로 노출한다.
    """

    result: LearnResult
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


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
    """Two-stage learner (design doc §4.3).

    Stage 1 classifies naming statistics into rules on the cheap learn tier.
    Stage 2 escalates one reasoning sub-task — naming a hidden split's boundary —
    to the strong tier, but only when a split was actually detected (in code, by
    the extractor) and only for the split groups. Most runs make a single cheap
    call; a run with a split pays one small extra call. This replaces the old
    behaviour where a detected split only printed "re-run with a better model".
    """

    def __init__(
        self,
        model: str | None = None,
        *,
        escalate: bool = True,
        reasoning_model: str | None = None,
    ):
        self.model = model or default_learn_model()
        self.reasoning_model = reasoning_model or default_reasoning_model()
        # Escalating to the same model buys nothing — a run that already uses the
        # strong model for stage 1 skips stage 2.
        self.escalate = escalate and self.reasoning_model != self.model
        self.client = get_client()  # provider from LLM_PROVIDER; key from env

    def learn(
        self, stats: list[CategoryStats], scan_scope: RuleScope | None = None
    ) -> LearnResult:
        """채택/기각 규칙만 필요할 때. 토큰 사용량이 필요하면 learn_with_usage()."""
        return self.learn_with_usage(stats, scan_scope).result

    def learn_with_usage(
        self, stats: list[CategoryStats], scan_scope: RuleScope | None = None
    ) -> LearnOutcome:
        """learn()과 같되 두 단계의 토큰 사용량을 합산해 함께 돌려준다.

        사용량은 예전에 로그로만 남았다 — 검증/비용 집계처럼 usage가 필요한 호출자가
        provider 클라이언트를 직접 부르지 않아도 되도록 공개 API로 노출한다. 승급이
        일어나면 1·2단계 토큰을 더해 돌려주므로 비용이 한 숫자로 잡힌다.
        """
        # Stage 1 — structured classification on the cheap learn tier.
        parsed = self.client.parse(
            model=self.model,
            max_tokens=8000,
            system=_SYSTEM_PROMPT_TEMPLATE.format(
                min_occ=MIN_RULE_OCCURRENCES, min_cons=MIN_RULE_CONSISTENCY
            ),
            user=_render_stats_text(stats),
            # Passed explicitly (None → provider default) so the difference from
            # the review stage is a recorded decision, not an oversight.
            temperature=LEARN_TEMPERATURE,
            schema=LearnResult,
        )
        result = parsed.parsed
        if result is None:
            raise RuntimeError("LLM returned no parseable convention output")
        input_tokens, output_tokens = parsed.input_tokens, parsed.output_tokens

        # Stage 2 — escalate the reasoning sub-task to the strong tier. The split
        # was already found in code (extractor.detect_split_signal); the strong
        # model is asked only to *explain* it, over just the split groups.
        split_text = _render_splits_text(stats)
        if split_text and self.escalate:
            log.info(
                "hidden split(s) detected — escalating boundary naming to %s",
                self.reasoning_model,
            )
            adj = self.client.parse(
                model=self.reasoning_model,
                max_tokens=2000,
                system=_SPLIT_SYSTEM_PROMPT,
                user=split_text,
                temperature=LEARN_TEMPERATURE,
                schema=SplitAdjudication,
            )
            hypotheses = adj.parsed.split_hypotheses if adj.parsed else []
            input_tokens += adj.input_tokens
            output_tokens += adj.output_tokens
        else:
            # No split, or escalation off: stage 1 is asked not to produce these,
            # so this is normally empty — but honour it if a caller kept it on.
            hypotheses = result.split_hypotheses

        log.info(
            "LLM proposed %d rule(s), %d rejection(s), %d split hypothesis(es); "
            "tokens in=%d out=%d",
            len(result.rules),
            len(result.rejected),
            len(hypotheses),
            input_tokens,
            output_tokens,
        )
        # Rules can only be trusted where they were measured: a scan narrowed to
        # a subtree yields rules scoped to that subtree. Assigned in code, never
        # taken from the model, for the same reason as the threshold gate.
        scope = scan_scope or RuleScope()
        for rule in result.rules:
            rule.scope = scope.model_copy(deep=True)
        gated = apply_threshold_gate(result)
        return LearnOutcome(
            result=LearnResult(
                rules=gated.rules,
                rejected=gated.rejected,
                split_hypotheses=hypotheses,
            ),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


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


def _facet_axes(s: CategoryStats):
    """The (facet, counts, denominator) triples a split check iterates over.

    Casing uses its own smaller denominator (single-word names carry no casing
    signal); prefix and suffix use the category total."""
    return (
        ("prefix", s.prefix_counts, s.total),
        ("suffix", s.suffix_counts, s.total),
        ("casing", s.casing_counts, s.casing_informative),
    )


def _render_stats_text(stats: list[CategoryStats]) -> str:
    """Stage-1 input: clean statistics, no split markers.

    Split markers were removed here on purpose — naming a split is stage 2's job
    now, and the threshold gate rejects a split category regardless of whether
    the marker is shown. Keeping stage 1 focused on classification is the whole
    point of the two-stage split (design doc §4.1)."""
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
        parts.append("")
    return "\n".join(parts)


def _render_splits_text(stats: list[CategoryStats]) -> str:
    """Stage-2 input: only the categories that look like two groups.

    Empty string when nothing is split — the caller uses that to skip the whole
    escalation call, so most runs never reach the strong model."""
    blocks: list[str] = []
    for s in stats:
        for facet, counts, total in _facet_axes(s):
            marker = _split_marker(s, facet, counts, total)
            if marker:
                blocks.append(marker)
    return "\n\n".join(blocks)


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
