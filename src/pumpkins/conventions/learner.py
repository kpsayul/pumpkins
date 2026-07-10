"""Convention learning, stage L2 — LLM judgment + threshold gate + YAML render.

Receives the naming statistics from stage L1 (extractor.py) and asks Claude to
judge which patterns are actual *rules* of the project. The LLM's job here is
deliberately narrow — structured stats in, structured rules out — which is why
the cheaper DEFAULT_LEARN_MODEL suffices (docs/convention-detection-design.md
§4). The review pipeline keeps the stronger model.

Two safeguards from the design doc:
- threshold gate (§3-(2)) is enforced *in code*, not just in the prompt — a
  rule below MIN_RULE_OCCURRENCES / MIN_RULE_CONSISTENCY is demoted to a
  rejected candidate no matter what the LLM says.
- the output is a human-reviewable conventions.yml (§3-(1)) meant to be
  committed to the target repo; people can edit or delete rules.

Auth: the anthropic SDK reads ANTHROPIC_API_KEY from the environment.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import anthropic
import yaml
from pydantic import BaseModel, Field

from pumpkins.config import (
    DEFAULT_LEARN_MODEL,
    MIN_RULE_CONSISTENCY,
    MIN_RULE_OCCURRENCES,
)
from pumpkins.conventions.extractor import CategoryStats

log = logging.getLogger(__name__)

_SYSTEM_PROMPT_TEMPLATE = """\
You are analyzing identifier-naming statistics extracted from a C++ repository
to discover the project's *implicit* naming conventions — rules the team
follows in code even though they may be written down nowhere.

You receive, per identifier category (member_variable / function /
class_type): a total count, distributions over prefixes, suffixes and casing
styles, and a sample of raw names.

Adopt a pattern as a rule ONLY when the statistics clearly support it:
- at least {min_occ} occurrences in the category, AND
- the dominant pattern covers at least {min_cons:.0%} of the category.
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

Report rejected candidates briefly with a reason. Be conservative: every wrong
rule becomes a false review comment later, and false comments are what make
users turn the tool off.
"""


class ConventionRule(BaseModel):
    """One adopted naming rule — a row in conventions.yml.

    facet/value make the rule machine-checkable: the review pipeline compares
    an identifier's split_pattern() facets against them deterministically
    (conventions/checker.py). facet="other" rules are human-readable only.
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


class RejectedCandidate(BaseModel):
    """A pattern considered but not adopted — kept in the file for transparency."""

    category: str
    pattern: str
    reason: str


class LearnResult(BaseModel):
    rules: list[ConventionRule]
    rejected: list[RejectedCandidate] = Field(default_factory=list)


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
    return LearnResult(rules=kept, rejected=rejected)


class ConventionLearner:
    def __init__(self, model: str = DEFAULT_LEARN_MODEL):
        self.model = model
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    def learn(self, stats: list[CategoryStats]) -> LearnResult:
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=8000,
            system=_SYSTEM_PROMPT_TEMPLATE.format(
                min_occ=MIN_RULE_OCCURRENCES, min_cons=MIN_RULE_CONSISTENCY
            ),
            messages=[{"role": "user", "content": _render_stats_text(stats)}],
            output_format=LearnResult,
        )
        result = response.parsed_output
        if result is None:
            raise RuntimeError("LLM returned no parseable convention output")
        log.info(
            "LLM proposed %d rule(s), %d rejection(s); tokens in=%d out=%d",
            len(result.rules),
            len(result.rejected),
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        return apply_threshold_gate(result)


# ----------------------------------------------------------------- rendering

def _render_stats_text(stats: list[CategoryStats]) -> str:
    parts = ["## Identifier statistics\n"]
    for s in stats:
        parts.append(f"### {s.category} (total {s.total})")
        parts.append(f"prefixes: {_fmt_counts(s.prefix_counts, s.total)}")
        parts.append(f"suffixes: {_fmt_counts(s.suffix_counts, s.total)}")
        parts.append(f"casing:   {_fmt_counts(s.casing_counts, s.total)}")
        parts.append(f"samples:  {', '.join(s.samples) or '(none)'}\n")
    return "\n".join(parts)


def _fmt_counts(counts: dict[str, int], total: int) -> str:
    if not counts:
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


def render_conventions_yaml(
    repo: Path, model: str, stats: list[CategoryStats], result: LearnResult
) -> str:
    """Render the committable conventions.yml (design doc §3-(1))."""
    header = (
        "# generated by `pumpkins learn` — 이 파일이 리뷰 지적의 근거가 됩니다.\n"
        "# 사람이 검수·수정해서 커밋하세요: 규칙을 고치면 지적이 바뀌고, 지우면 사라집니다.\n"
    )
    doc = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "repo": str(repo),
        "thresholds": {
            "min_occurrences": MIN_RULE_OCCURRENCES,
            "min_consistency": MIN_RULE_CONSISTENCY,
        },
        "rules": [r.model_dump() for r in result.rules],
        "rejected_candidates": [r.model_dump() for r in result.rejected],
        # raw distributions kept for transparency — "왜 이 규칙이야?"의 근거
        "stats_summary": {
            s.category: {
                "total": s.total,
                "prefixes": s.prefix_counts,
                "suffixes": s.suffix_counts,
                "casing": s.casing_counts,
            }
            for s in stats
        },
    }
    return header + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
