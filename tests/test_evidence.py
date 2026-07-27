"""Tests for finding provenance — the label on every finding.

Two questions a review tool must be able to answer about anything it says:
"which rule made you say this?" and "would you say it again?". Before Evidence
existed the first lived in a string with three different formats and the second
was not recorded at all — so a deterministic rule violation and a model's
one-off guess were indistinguishable in the output.
"""

import pytest

from pumpkins.models import (
    DETERMINISTIC_DETECTORS,
    DetectorKind,
    Evidence,
    Finding,
    ReviewResult,
    Severity,
)
from pumpkins.report import render_markdown


def _finding(evidence: Evidence, title: str = "something") -> Finding:
    return Finding(file="src/a.cpp", line=1, title=title, explanation="…", evidence=evidence)


# ------------------------------------------------------- reproducibility rule

@pytest.mark.parametrize(
    "detector, expected",
    [
        (DetectorKind.clang_tidy, True),
        (DetectorKind.convention, True),
        (DetectorKind.llm, False),
    ],
)
def test_reproducible_is_derived_from_the_detector(detector, expected):
    """A producer cannot forget the flag or contradict its own detector."""
    assert Evidence(detector=detector).reproducible is expected


def test_a_producer_may_still_override_reproducibility():
    """clang-tidy findings that survived LLM triage are the real case: the
    diagnostic is deterministic, its presence in the report is not."""
    evidence = Evidence(
        detector=DetectorKind.clang_tidy, reproducible=False, model="gpt-4o"
    )
    assert evidence.reproducible is False
    assert evidence.rule_id is None


def test_only_deterministic_detectors_are_marked_reproducible_by_default():
    """This set is what a CI gate is allowed to fail on. Widening it silently
    would let a model-dependent verdict break a build."""
    assert DETERMINISTIC_DETECTORS == {DetectorKind.clang_tidy, DetectorKind.convention}
    assert DetectorKind.llm not in DETERMINISTIC_DETECTORS


def test_a_finding_cannot_exist_without_provenance():
    with pytest.raises(Exception):
        Finding(file="a.cpp", line=1, title="t", explanation="e")


def test_evidence_survives_serialization():
    """findings.json must carry the label — that is what makes two runs comparable."""
    dumped = Evidence(
        detector=DetectorKind.convention, rule_id="member-prefix-m", occurrences=187,
        coverage=0.92, rule_scope="제외 src/legacy",
    ).model_dump()
    assert dumped["rule_id"] == "member-prefix-m"
    assert dumped["reproducible"] is True
    restored = Evidence.model_validate(dumped)
    assert restored.coverage == 0.92 and restored.rule_scope == "제외 src/legacy"


# --------------------------------------------------------------- report label

def test_report_labels_a_deterministic_finding():
    result = ReviewResult(
        analyzed_files=1,
        findings=[
            _finding(
                Evidence(
                    detector=DetectorKind.convention, rule_id="member-prefix-m",
                    occurrences=187, coverage=0.92,
                )
            )
        ],
    )
    report = render_markdown(result)
    assert "규칙 `member-prefix-m`" in report
    assert "재현 가능" in report
    assert "근거 187개 중 92%" in report


def test_report_labels_a_model_dependent_finding():
    result = ReviewResult(
        analyzed_files=1,
        findings=[_finding(Evidence(detector=DetectorKind.llm, model="gpt-4o"))],
    )
    report = render_markdown(result)
    assert "규칙 없음" in report
    assert "`gpt-4o`" in report
    assert "재현 보장 안 됨" in report


def test_report_summarizes_the_split():
    """The number a CI policy is written against."""
    result = ReviewResult(
        analyzed_files=1,
        findings=[
            _finding(Evidence(detector=DetectorKind.convention, rule_id="r1"), "a"),
            _finding(Evidence(detector=DetectorKind.convention, rule_id="r2"), "b"),
            _finding(Evidence(detector=DetectorKind.llm, model="gpt-4o"), "c"),
        ],
    )
    report = render_markdown(result)
    assert "재현 가능 2건" in report
    assert "모델 의존 1건" in report


def test_scope_is_shown_only_when_the_rule_is_narrowed():
    repo_wide = render_markdown(
        ReviewResult(
            findings=[
                _finding(
                    Evidence(
                        detector=DetectorKind.convention, rule_id="r", rule_scope="리포 전체"
                    )
                )
            ]
        )
    )
    assert "적용 리포 전체" not in repo_wide  # 기본값을 매번 반복하지 않는다

    narrowed = render_markdown(
        ReviewResult(
            findings=[
                _finding(
                    Evidence(
                        detector=DetectorKind.convention, rule_id="r",
                        rule_scope="제외 src/legacy",
                    )
                )
            ]
        )
    )
    assert "적용 제외 src/legacy" in narrowed
