"""Mechanical verification of AI-inferred rules — measure a check against the
repo and gate it. Pure functions over temp files; no API key."""

from pumpkins.conventions import (
    InferredRule,
    RuleCheck,
    verify,
    verify_inferred,
)


def _members(prefix: str, n: int) -> str:
    body = "\n".join(f"    int {prefix}{i};" for i in range(n))
    # a real class name — a single capital letter reads as an ALL_CAPS macro
    return f"class Widget {{\nprivate:\n{body}\n}};\n"


def test_verify_naming_prefix_measures_real_coverage(tmp_path):
    # 3 of 4 members use m_ → 75%
    (tmp_path / "a.h").write_text(
        "class Widget {\nprivate:\n  int m_a;\n  int m_b;\n  int m_c;\n  int plain;\n};\n",
        encoding="utf-8",
    )
    result = verify(tmp_path, RuleCheck(kind="naming", category="member", facet="prefix", value="m_"))
    assert result.matches == 3 and result.total == 4
    assert result.coverage == 0.75


def test_verify_rejects_a_suffix_the_code_does_not_have(tmp_path):
    # the live 'pointer members end in Ptr' guess: no member ends in Ptr → 0%
    (tmp_path / "a.h").write_text(_members("m_", 5), encoding="utf-8")
    result = verify(tmp_path, RuleCheck(kind="naming", category="member", facet="suffix", value="Ptr"))
    assert result.matches == 0
    assert result.coverage == 0.0


def test_verify_header_directive(tmp_path):
    (tmp_path / "yes.h").write_text("#pragma once\nclass A {};\n", encoding="utf-8")
    (tmp_path / "no.h").write_text("#ifndef X\n#define X\nclass B {};\n#endif\n", encoding="utf-8")
    result = verify(tmp_path, RuleCheck(kind="header_directive", text="#pragma once"))
    assert result.matches == 1 and result.total == 2
    assert result.coverage == 0.5


def test_verify_returns_none_for_unrunnable_check(tmp_path):
    (tmp_path / "a.h").write_text(_members("m_", 3), encoding="utf-8")
    assert verify(tmp_path, RuleCheck(kind="none")) is None
    assert verify(tmp_path, RuleCheck(kind="naming", facet="", value="")) is None


def test_verify_inferred_promotes_a_true_naming_rule(tmp_path):
    # 20 members all m_ → passes coverage AND the occurrence gate → verified
    (tmp_path / "a.h").write_text(_members("m_", 20), encoding="utf-8")
    inferred = [
        InferredRule(
            rule="멤버는 m_ 접두어를 쓴다",
            check=RuleCheck(kind="naming", category="member", facet="prefix", value="m_"),
        )
    ]
    report = verify_inferred(tmp_path, inferred)
    assert len(report.verified) == 1 and not report.rejected
    rule = report.verified[0]
    # a verified naming rule becomes a real facet rule the checker enforces
    assert rule.facet == "prefix" and rule.value == "m_"
    assert rule.category == "member_variable"
    assert rule.coverage == 1.0 and rule.occurrences == 20


def test_verify_inferred_rejects_a_wrong_guess(tmp_path):
    (tmp_path / "a.h").write_text(_members("m_", 20), encoding="utf-8")
    inferred = [
        InferredRule(
            rule="포인터 멤버는 Ptr 접미사를 쓴다",
            check=RuleCheck(kind="naming", category="member", facet="suffix", value="Ptr"),
        )
    ]
    report = verify_inferred(tmp_path, inferred)
    assert not report.verified
    assert len(report.rejected) == 1
    assert "0%" in report.rejected[0][1]


def test_verify_inferred_keeps_uncheckable_guess_as_unverified(tmp_path):
    (tmp_path / "a.h").write_text(_members("m_", 3), encoding="utf-8")
    inferred = [InferredRule(rule="에러는 코드로 반환한다", kind="error-handling")]  # check kind=none
    report = verify_inferred(tmp_path, inferred)
    assert not report.verified and not report.rejected
    assert len(report.unverified) == 1
    assert report.unverified[0].facet == "other"
