"""Tests for report rendering, focused on coverage honesty.

The failure mode these guard against: a report showing zero findings when the
pipeline never read the change. On a real PR (fmt #4865) the file carrying the
whole logic change was a header, silently unanalyzable in shallow mode, and the
report still said "No findings" — indistinguishable from a clean pass.
"""

from pumpkins.models import DetectorKind, Evidence, Finding, ReviewResult, Severity
from pumpkins.report import render_markdown


def test_clean_run_with_full_coverage_passes_plainly():
    report = render_markdown(ReviewResult(analyzed_files=2))
    assert "✅" in report
    assert "검사하지 못한" not in report


def test_zero_findings_with_skipped_header_is_not_a_pass():
    result = ReviewResult(
        shallow_mode=True,
        analyzed_files=1,
        skipped_headers=["include/fmt/format.h"],
    )
    report = render_markdown(result)
    assert "✅" not in report
    assert "⚠️" in report
    assert "include/fmt/format.h" in report
    assert "통과로 읽지 마세요" in report


def test_non_cpp_changes_are_listed_as_unread():
    result = ReviewResult(
        analyzed_files=1,
        skipped_non_cpp=["model/x/Widget.tc", "model/x/Widget.yml"],
    )
    report = render_markdown(result)
    assert "Widget.tc" in report and "Widget.yml" in report
    assert "✅" not in report


def test_coverage_line_reports_the_ratio():
    result = ReviewResult(analyzed_files=1, skipped_headers=["a.h", "b.hpp"])
    assert "1/3 changed C++ file(s) statically analyzed" in render_markdown(result)


def test_findings_still_render_alongside_the_coverage_warning():
    result = ReviewResult(
        analyzed_files=1,
        skipped_non_cpp=["notes.md"],
        findings=[
            Finding(
                file="src/a.cpp",
                line=7,
                severity=Severity.high,
                title="data race on m_count",
                explanation="two threads write without the mutex held",
                evidence=Evidence(detector=DetectorKind.llm, model="gpt-4o"),
            )
        ],
    )
    report = render_markdown(result)
    assert "data race on m_count" in report
    assert "notes.md" in report


def test_no_coverage_section_when_nothing_was_skipped():
    assert not ReviewResult(analyzed_files=3).has_coverage_gap
