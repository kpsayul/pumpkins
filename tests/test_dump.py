"""Tests for run artifacts (`--out-dir`).

The point of this directory is attribution: a result you cannot tie to a model,
a rule set and a tool version is a result you cannot compare across models —
which is the product's whole claim.
"""

import json

from pumpkins.conventions import StoredRule, write_rule
from pumpkins.models import (
    DetectorKind,
    Evidence,
    Finding,
    RawDiagnostic,
    ReviewResult,
    Severity,
)
from pumpkins.report import render_markdown
from pumpkins.report.dump import RunContext, dump_run, rules_fingerprint


def _rule(value: str = "m_") -> StoredRule:
    return StoredRule(
        id=f"member-prefix-{value}",
        category="member_variable",
        description="멤버 변수 접두사",
        facet="prefix",
        value=value,
        coverage=0.92,
        occurrences=187,
        confidence="high",
    )


def _dump(tmp_path, result, context=None):
    out = tmp_path / "out"
    dump_run(out, tmp_path, result, render_markdown(result), context or RunContext())
    return out


def test_dump_writes_the_expected_artifacts(tmp_path):
    result = ReviewResult(analyzed_files=1, llm_used=False)
    out = _dump(tmp_path, result)
    assert {p.name for p in out.rglob("*") if p.is_file()} == {
        "report.md", "run.json", "diff.patch", "diagnostics.json", "findings.json",
    }


def test_run_json_records_what_produced_the_result(tmp_path):
    result = ReviewResult(
        llm_used=True, provider="openai", model="gpt-4o", temperature=0.0,
        profile="concurrency", analyzed_files=2, shallow_mode=True,
    )
    out = _dump(tmp_path, result, RunContext(tool_version="14.0.0"))

    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    # temperature belongs here too: it changes the result, so a run you cannot
    # attribute to a sampling setting is a run you cannot compare.
    assert run["llm"] == {
        "used": True, "provider": "openai", "model": "gpt-4o", "temperature": 0.0,
    }
    assert run["clang_tidy_version"] == "14.0.0"
    assert run["analysis_mode"] == "shallow"
    assert run["pumpkins_version"]


def test_run_json_reports_incomplete_coverage(tmp_path):
    """The coverage gap must be machine-readable too, not just prose in the report."""
    result = ReviewResult(analyzed_files=1, skipped_headers=["a.h"], skipped_non_cpp=["b.md"])
    run = json.loads((_dump(tmp_path, result) / "run.json").read_text(encoding="utf-8"))
    assert run["coverage"]["complete"] is False
    assert run["coverage"]["skipped_headers"] == ["a.h"]


def test_llm_artifacts_only_appear_when_the_llm_ran(tmp_path):
    plain = _dump(tmp_path / "a", ReviewResult())
    assert not (plain / "llm").exists()

    withllm = _dump(
        tmp_path / "b",
        ReviewResult(llm_used=True, provider="openai", model="gpt-4o"),
        RunContext(llm_request="system+user text", llm_response={"verdicts": []}),
    )
    assert (withllm / "llm" / "request.txt").read_text(encoding="utf-8") == "system+user text"
    assert json.loads((withllm / "llm" / "response.json").read_text(encoding="utf-8")) == {
        "verdicts": []
    }


def test_stale_artifacts_from_a_previous_run_are_cleared(tmp_path):
    """A leftover prompt from an earlier LLM run would misattribute a --no-llm run."""
    out = tmp_path / "out"
    dump_run(
        out, tmp_path, ReviewResult(llm_used=True), "report",
        RunContext(llm_request="old prompt"),
    )
    assert (out / "llm" / "request.txt").exists()

    dump_run(out, tmp_path, ReviewResult(), "report", RunContext())
    assert not (out / "llm" / "request.txt").exists()


def test_findings_are_dumped_structured(tmp_path):
    result = ReviewResult(
        findings=[
            Finding(
                file="src/a.cpp", line=7, severity=Severity.low,
                title="`count`", explanation="…",
                evidence=Evidence(
                    detector=DetectorKind.convention, rule_id="member-prefix-m_"
                ),
            )
        ]
    )
    findings = json.loads((_dump(tmp_path, result) / "findings.json").read_text(encoding="utf-8"))
    assert findings[0]["evidence"]["rule_id"] == "member-prefix-m_"
    assert findings[0]["evidence"]["detector"] == "convention"
    assert findings[0]["evidence"]["reproducible"] is True


def test_diagnostics_are_dumped_before_triage(tmp_path):
    context = RunContext(
        diagnostics=[
            RawDiagnostic(file="a.cpp", line=3, check="concurrency-mt-unsafe", message="mt-unsafe")
        ]
    )
    out = _dump(tmp_path, ReviewResult(), context)
    dumped = json.loads((out / "diagnostics.json").read_text(encoding="utf-8"))
    assert dumped[0]["check"] == "concurrency-mt-unsafe"


# ------------------------------------------------------------- fingerprinting

def test_fingerprint_is_stable_for_unchanged_rules(tmp_path):
    root = tmp_path / "conventions"
    write_rule(root, "active", _rule())
    assert rules_fingerprint(root) == rules_fingerprint(root)


def test_fingerprint_changes_when_a_rule_changes(tmp_path):
    root = tmp_path / "conventions"
    write_rule(root, "active", _rule())
    before = rules_fingerprint(root)

    write_rule(root, "active", _rule(value="m"))
    assert rules_fingerprint(root) != before


def test_fingerprint_ignores_unapproved_candidates(tmp_path):
    """Candidates are not enforced, so they must not change the run's identity."""
    root = tmp_path / "conventions"
    write_rule(root, "active", _rule())
    before = rules_fingerprint(root)

    write_rule(root, "candidate", _rule(value="s_"))
    assert rules_fingerprint(root) == before


def test_fingerprint_is_none_without_rules(tmp_path):
    assert rules_fingerprint(None) is None
    assert rules_fingerprint(tmp_path / "nope") is None
