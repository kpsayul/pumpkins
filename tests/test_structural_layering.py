"""계층 방향(include_direction)과 소유권(member_ownership) — 통계로는 못 찾는 규칙.

두 검사가 다른 점이 하나 있다: 계층 검사는 tree-sitter 없이도 돌아야 한다.
`#include` 는 한 줄로 읽히고, "어느 계층이 어느 계층을 참조해도 되는가"는 네이티브
빌드 실패로 잃기에는 너무 중요한 규칙이라서다."""

import os

import pytest

from pumpkins.conventions import (
    ConventionRule,
    InferredRule,
    RuleCheck,
    check_structural,
    verify,
    verify_inferred,
)
from pumpkins.conventions import checker as checker_mod
from pumpkins.languages.cpp import ast as cpp_ast
from pumpkins.models import DiffScope, FileDiff, LineRange

_ALLOW_MISSING = os.environ.get("PUMPKINS_ALLOW_NO_TREE_SITTER") == "1"
if not cpp_ast.available() and not _ALLOW_MISSING:
    pytest.fail(
        f"tree-sitter unavailable — {cpp_ast.unavailable_reason()}. "
        "구조 검사 전체가 검증되지 않은 상태입니다. 설치하거나, 의도한 것이라면 "
        "PUMPKINS_ALLOW_NO_TREE_SITTER=1 로 명시하세요.",
        pytrace=False,
    )

requires_ts = pytest.mark.skipif(
    not cpp_ast.available(), reason="PUMPKINS_ALLOW_NO_TREE_SITTER=1 (명시적 건너뛰기)"
)


# --------------------------------------------------------------- 계층 방향 측정

def _layered_repo(tmp_path, ui_files: int = 22, leaks: int = 0):
    """core 는 독립, ui 는 core 를 참조. leaks 만큼 core 가 ui 를 거꾸로 참조한다."""
    (tmp_path / "src" / "core").mkdir(parents=True)
    (tmp_path / "src" / "ui").mkdir(parents=True)
    for i in range(ui_files):
        (tmp_path / "src" / "ui" / f"w{i}.h").write_text(
            f'#pragma once\n#include "engine.h"\nclass W{i} {{}};\n', encoding="utf-8"
        )
    for i in range(ui_files):
        body = '#pragma once\n'
        if i < leaks:
            body += f'#include "w{i}.h"\n'
        (tmp_path / "src" / "core" / f"engine{i}.h").write_text(
            body + f"class E{i} {{}};\n", encoding="utf-8"
        )
    (tmp_path / "src" / "core" / "engine.h").write_text("#pragma once\n", encoding="utf-8")
    return tmp_path


def test_a_one_way_dependency_measures_as_a_layering_rule(tmp_path):
    repo = _layered_repo(tmp_path, ui_files=22)
    result = verify(repo, RuleCheck(
        kind="include_direction", from_dir="src/core", forbidden_dir="src/ui"
    ))
    assert result.matches == result.total == 23   # core 전부가 ui 를 참조하지 않는다
    assert result.coverage == 1.0


def test_the_denominator_is_the_layer_not_the_repo(tmp_path):
    """레포 전체를 분모로 삼으면 큰 무관한 코드베이스가 어떤 방향 규칙이든
    공짜로 통과시킨다 — 이 프로젝트가 반복해서 치른 분모 실수."""
    repo = _layered_repo(tmp_path, ui_files=22, leaks=11)
    result = verify(repo, RuleCheck(
        kind="include_direction", from_dir="src/core", forbidden_dir="src/ui"
    ))
    assert result.total == 23           # ui 22개는 분모에 없다
    assert result.matches == 12         # 11개가 거꾸로 참조 중
    assert result.coverage < 0.85       # → 게이트에서 기각


def test_a_layer_the_scan_does_not_contain_is_unmeasurable(tmp_path):
    """빈 계층을 '완벽히 지켜짐'으로 세면 없는 규칙이 생긴다."""
    repo = _layered_repo(tmp_path, ui_files=3)
    assert verify(repo, RuleCheck(
        kind="include_direction", from_dir="src/core", forbidden_dir="src/nonexistent"
    )) is None


def test_layering_is_measured_without_tree_sitter(tmp_path, monkeypatch):
    """네이티브 파서가 죽어도 계층 규칙은 살아 있어야 한다."""
    repo = _layered_repo(tmp_path, ui_files=22)
    monkeypatch.setattr("pumpkins.conventions.verifier.cpp_ast.require", lambda p: False)
    result = verify(repo, RuleCheck(
        kind="include_direction", from_dir="src/core", forbidden_dir="src/ui"
    ))
    assert result is not None and result.coverage == 1.0


def test_a_true_layering_guess_is_promoted(tmp_path):
    repo = _layered_repo(tmp_path, ui_files=22)
    inferred = [InferredRule(
        rule="core 계층은 ui 계층에 의존하지 않는다", kind="structural",
        check=RuleCheck(kind="include_direction", from_dir="src/core", forbidden_dir="src/ui"),
    )]
    report = verify_inferred(repo, inferred)
    assert len(report.verified) == 1 and not report.rejected
    assert report.verified[0].check.kind == "include_direction"


def test_too_few_samples_reads_differently_from_contradiction(tmp_path):
    """'레포가 반박한다'와 '표본이 모자라 판단 불가'는 사람이 할 행동이 다르다."""
    repo = _layered_repo(tmp_path, ui_files=4)   # core 5개 — 게이트(20) 미달
    inferred = [InferredRule(
        rule="core 는 ui 에 의존하지 않는다",
        check=RuleCheck(kind="include_direction", from_dir="src/core", forbidden_dir="src/ui"),
    )]
    (_, reason), = verify_inferred(repo, inferred).rejected
    assert "우연과 구분 불가" in reason and "100%" in reason


# ------------------------------------------------------------------ 소유권 측정

_OWNERSHIP_SRC = (
    "class A {\n"
    "  std::unique_ptr<X> m_owned;\n"      # smart
    "  std::shared_ptr<Y> m_shared;\n"     # smart
    "  Widget* m_raw;\n"                   # raw
    "  int m_count;\n"                     # 값 — 분모 밖
    "  const char* m_name;\n"              # C 문자열 — 소유권이 아니다
    "  Thing& m_ref;\n"                    # 참조 — 소유하지 않는다
    "};\n"
)


@requires_ts
def test_ownership_denominator_is_pointer_holding_members_only(tmp_path):
    (tmp_path / "a.h").write_text(_OWNERSHIP_SRC, encoding="utf-8")
    result = verify(tmp_path, RuleCheck(kind="member_ownership", value="smart"))
    # 분모 3 = smart 2 + raw 1. int/const char*/참조는 소유권 질문 자체가 아니다.
    assert result.total == 3 and result.matches == 2


@requires_ts
def test_ast_reads_ownership_shape_off_a_member():
    members = {m.name: m for m in cpp_ast.members(_OWNERSHIP_SRC)}
    assert members["m_owned"].is_smart_pointer and not members["m_owned"].is_raw_pointer
    assert members["m_raw"].is_raw_pointer and members["m_raw"].owner == "A"
    assert not members["m_count"].holds_pointer
    assert not members["m_name"].holds_pointer   # const char* 는 뷰지 소유가 아니다
    assert not members["m_ref"].holds_pointer


@requires_ts
def test_ownership_cannot_be_measured_without_tree_sitter(tmp_path, monkeypatch):
    (tmp_path / "a.h").write_text(_OWNERSHIP_SRC, encoding="utf-8")
    monkeypatch.setattr("pumpkins.conventions.verifier.cpp_ast.require", lambda p: False)
    assert verify(tmp_path, RuleCheck(kind="member_ownership", value="smart")) is None


# --------------------------------------------------------------- 상속 관계 측정

@requires_ts
def test_hierarchy_rule_is_measured_over_matching_classes_only(tmp_path):
    """분모는 이름이 규칙에 해당하는 클래스뿐 — 레포의 모든 클래스가 아니다."""
    (tmp_path / "e.h").write_text(
        "\n".join(
            f"class E{i}Exception : public std::runtime_error {{}};" for i in range(21)
        )
        + "\nclass BadException : public SomethingElse {};\n"
        + "\nclass NotRelated {};\n",
        encoding="utf-8",
    )
    result = verify(tmp_path, RuleCheck(
        kind="base_class", name_suffix="Exception", base_contains="runtime_error"
    ))
    assert result.total == 22 and result.matches == 21   # NotRelated 는 분모 밖


@requires_ts
def test_hierarchy_guess_is_promoted_when_the_repo_backs_it(tmp_path):
    """yaml-cpp 실측에서 '예외 클래스는 std::runtime_error 를 상속한다'가 나왔는데
    돌릴 검사가 없어 미검증으로 남았다 — 그래서 이 검사를 추가했다."""
    (tmp_path / "e.h").write_text(
        "\n".join(
            f"class E{i}Exception : public std::runtime_error {{}};" for i in range(20)
        ),
        encoding="utf-8",
    )
    inferred = [InferredRule(
        rule="예외 클래스는 std::runtime_error를 상속한다", kind="structural",
        check=RuleCheck(kind="base_class", name_suffix="Exception",
                        base_contains="runtime_error"),
    )]
    report = verify_inferred(tmp_path, inferred)
    assert len(report.verified) == 1 and report.verified[0].coverage == 1.0


# --------------------------------------------------------- 리뷰 쪽 결정적 검사

def _layering_rule() -> ConventionRule:
    return ConventionRule(
        id="ai-core-independent", category="structural",
        description="core 계층은 ui 계층에 의존하지 않는다", facet="other",
        coverage=1.0, occurrences=23, confidence="high",
        check=RuleCheck(kind="include_direction", from_dir="src/core", forbidden_dir="src/ui"),
    )


_ADD_BAD_INCLUDE = """\
diff --git a/src/core/engine.h b/src/core/engine.h
index 1111111..2222222 100644
--- a/src/core/engine.h
+++ b/src/core/engine.h
@@ -1,2 +1,3 @@
 #pragma once
+#include "w1.h"
 class E {};
"""


def test_review_flags_an_added_include_that_crosses_a_layer(tmp_path):
    _layered_repo(tmp_path, ui_files=3)
    scope = DiffScope(files=[FileDiff(
        path="src/core/engine.h", patch_text=_ADD_BAD_INCLUDE,
        added_ranges=[LineRange(start=2, end=2)],
    )])
    (finding,) = check_structural(scope, [_layering_rule()], tmp_path)
    assert finding.line == 2 and "w1.h" in finding.title
    assert finding.evidence.reproducible is True


# 계층을 넘는 include 가 이미 있던 파일에, 무관한 줄 하나를 추가한 diff.
_UNRELATED_EDIT = """\
diff --git a/src/core/engine.h b/src/core/engine.h
index 1111111..2222222 100644
--- a/src/core/engine.h
+++ b/src/core/engine.h
@@ -1,3 +1,4 @@
 #pragma once
 #include "w1.h"
 class E {};
+class F {};
"""


def test_review_ignores_a_crossing_include_that_was_already_there(tmp_path):
    """뒤늦게 채택한 계층 규칙이 남의 코드까지 전부 지적하면 아무도 안 쓴다."""
    _layered_repo(tmp_path, ui_files=3)
    scope = DiffScope(files=[FileDiff(
        path="src/core/engine.h", patch_text=_UNRELATED_EDIT,
        added_ranges=[LineRange(start=4, end=4)],
    )])
    # 진짜 파싱된 diff 인지 먼저 확인한다 — 깨진 patch 를 조용히 [] 로 처리하면
    # 이 테스트는 아무것도 증명하지 못한 채 통과한다 (실제로 한 번 그랬다).
    assert checker_mod._added_includes(scope.files[0]) == []
    assert check_structural(scope, [_layering_rule()], tmp_path) == []


def test_layering_is_enforced_at_review_without_tree_sitter(tmp_path, monkeypatch):
    _layered_repo(tmp_path, ui_files=3)
    monkeypatch.setattr(checker_mod.cpp_ast, "require", lambda p: False)
    scope = DiffScope(files=[FileDiff(
        path="src/core/engine.h", patch_text=_ADD_BAD_INCLUDE,
        added_ranges=[LineRange(start=2, end=2)],
    )])
    assert len(check_structural(scope, [_layering_rule()], tmp_path)) == 1


_ADD_RAW_MEMBER = """\
diff --git a/src/core/thing.h b/src/core/thing.h
index 1111111..2222222 100644
--- a/src/core/thing.h
+++ b/src/core/thing.h
@@ -1,3 +1,4 @@
 class Thing {
   std::unique_ptr<X> m_owned;
+  Widget* m_raw;
 };
"""


@requires_ts
def test_review_flags_a_new_raw_pointer_member(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "thing.h").write_text(
        "class Thing {\n  std::unique_ptr<X> m_owned;\n  Widget* m_raw;\n};\n",
        encoding="utf-8",
    )
    rule = ConventionRule(
        id="ai-smart-ownership", category="ownership",
        description="포인터 멤버는 스마트 포인터로 소유한다", facet="other",
        coverage=0.95, occurrences=40, confidence="high",
        check=RuleCheck(kind="member_ownership", value="smart"),
    )
    scope = DiffScope(files=[FileDiff(
        path="src/thing.h", patch_text=_ADD_RAW_MEMBER,
        added_ranges=[LineRange(start=3, end=3)],
    )])
    (finding,) = check_structural(scope, [rule], tmp_path)
    assert "m_raw" in finding.title and finding.line == 3
    assert "Thing::m_raw" in finding.explanation

    # 손대지 않은 줄은 지적하지 않는다 (m_owned 는 애초에 규칙을 지킨다)
    untouched = DiffScope(files=[FileDiff(
        path="src/thing.h", patch_text=_ADD_RAW_MEMBER,
        added_ranges=[LineRange(start=2, end=2)],
    )])
    assert check_structural(untouched, [rule], tmp_path) == []


_ADD_CLASS = """\
diff --git a/src/e.h b/src/e.h
index 1111111..2222222 100644
--- a/src/e.h
+++ b/src/e.h
@@ -1,1 +1,2 @@
 class AException : public std::runtime_error {};
+class BException : public SomethingElse {};
"""


@requires_ts
def test_review_flags_a_new_class_breaking_the_hierarchy(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "e.h").write_text(
        "class AException : public std::runtime_error {};\n"
        "class BException : public SomethingElse {};\n",
        encoding="utf-8",
    )
    rule = ConventionRule(
        id="ai-exception-base", category="structural",
        description="예외 클래스는 std::runtime_error를 상속한다", facet="other",
        coverage=0.95, occurrences=21, confidence="high",
        check=RuleCheck(kind="base_class", name_suffix="Exception",
                        base_contains="runtime_error"),
    )
    scope = DiffScope(files=[FileDiff(
        path="src/e.h", patch_text=_ADD_CLASS,
        added_ranges=[LineRange(start=2, end=2)],
    )])
    (finding,) = check_structural(scope, [rule], tmp_path)
    assert "BException" in finding.title and finding.line == 2
    assert "SomethingElse" in finding.explanation
