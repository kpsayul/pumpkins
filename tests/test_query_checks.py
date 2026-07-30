"""모델이 직접 쓴 검사(kind=query) — 열거를 끝내는 종류.

여기까지 모든 기계 검사는 제가 손으로 쓴 분기였다. `base_class` 가 존재하는
이유는 내가 지난주에 타이핑했기 때문이고, 그 전에는 "예외는 runtime_error 를
상속한다"가 옳은 관찰인데도 잴 방법이 없어 죽었다.

질의는 그 분기를 대체한다. 어휘가 목록이 아니라 언어가 된다."""

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
from pumpkins.languages.cpp import ast as cpp_ast, query as cpp_query
from pumpkins.models import DiffScope, FileDiff, LineRange

_ALLOW_MISSING = os.environ.get("PUMPKINS_ALLOW_NO_TREE_SITTER") == "1"
requires_ts = pytest.mark.skipif(
    not cpp_query.available(), reason="PUMPKINS_ALLOW_NO_TREE_SITTER=1 (명시적 건너뛰기)"
)

# 인자 없는 메서드 4개 중 2개만 const — 클래스마다 같은 비율
_KLASS = """\
namespace mylib {
class W%d {
 public:
  int width() const;
  int height() const;
  int depth();
  int volume();
  virtual void draw() override;
  virtual void hide();
};
}
"""

_NO_ARG_METHOD = (
    "(field_declaration (function_declarator "
    "declarator: (field_identifier) @subject (parameter_list)))"
)
_CONST_METHOD = (
    "(field_declaration (function_declarator "
    "declarator: (field_identifier) @subject (type_qualifier)))"
)


def _repo(tmp_path, n=6):
    (tmp_path / "src").mkdir(exist_ok=True)
    for i in range(n):
        (tmp_path / "src" / f"w{i}.h").write_text(_KLASS % i, encoding="utf-8")
    return tmp_path


# ------------------------------------------------------- 질의로 측정이 되는가

@requires_ts
def test_a_query_pair_measures_a_convention_no_branch_exists_for(tmp_path):
    """const 정확성은 손으로 쓴 검사가 하나도 없는 관행이다 — 그런데 잰다."""
    result = verify(_repo(tmp_path), RuleCheck(
        kind="query",
        population_query=_NO_ARG_METHOD,
        conforming_query=_CONST_METHOD,
    ))
    # 클래스당 인자 없는 메서드 6개(const 2 + 비const 2 + virtual 2), 그중 const 2
    assert result.total == 36 and result.matches == 12


@requires_ts
def test_the_population_query_is_the_denominator(tmp_path):
    """분모는 규칙이 스스로 주장하는 모집단이다 — 우리가 정하는 게 아니다.

    조건 질의만 세면 "const 인 것 중 const 인 것"이 되어 항상 100% 가 된다."""
    repo = _repo(tmp_path)
    honest = verify(repo, RuleCheck(kind="query",
                                   population_query=_NO_ARG_METHOD,
                                   conforming_query=_CONST_METHOD))
    circular = verify(repo, RuleCheck(kind="query",
                                      population_query=_CONST_METHOD,
                                      conforming_query=_CONST_METHOD))
    assert honest.coverage < 0.85          # 정직한 분모 → 기각
    assert circular.coverage == 1.0        # 자기 자신을 분모로 → 무의미한 100%


@requires_ts
def test_anonymous_nodes_must_be_quoted(tmp_path):
    """`virtual` 은 익명 노드라 `(virtual)` 은 아무것도 안 잡는다. 조용히 0이 되는
    함정이라 어휘 목록이 이 구분을 가르쳐 준다."""
    repo = _repo(tmp_path)
    quoted = verify(repo, RuleCheck(
        kind="query",
        population_query='(field_declaration "virtual" (function_declarator '
                         "declarator: (field_identifier) @subject))",
        conforming_query="(field_declaration (function_declarator "
                         "declarator: (field_identifier) @subject (virtual_specifier)))",
    ))
    assert quoted.total == 12 and quoted.matches == 6      # virtual 2개 중 1개만 override

    unquoted = cpp_query.compile_query(
        "(field_declaration (virtual) (function_declarator "
        "declarator: (field_identifier) @subject))"
    )
    assert unquoted is None       # 컴파일 자체가 안 됨 → 측정 불가로 안전하게 떨어짐


@requires_ts
def test_a_text_predicate_narrows_the_population(tmp_path):
    (tmp_path / "e.h").write_text(
        "\n".join(f"class E{i}Exception : public std::runtime_error {{}};" for i in range(21))
        + "\nclass BadException : public Other {};\nclass NotRelated : public Base {};\n",
        encoding="utf-8",
    )
    result = verify(tmp_path, RuleCheck(
        kind="query",
        population_query='(class_specifier name: (type_identifier) @subject '
                         '(#match? @subject "Exception$"))',
        conforming_query='(class_specifier name: (type_identifier) @subject '
                         '(base_class_clause (qualified_identifier) @b) '
                         '(#match? @b "runtime_error"))',
    ))
    assert result.total == 22 and result.matches == 21   # NotRelated 는 분모 밖


# ------------------------------------------------------- 나쁜 질의는 안전한가

@requires_ts
@pytest.mark.parametrize("bad", [
    "(this is not a query",                    # 문법 오류
    "(class_specifier name: (type_identifier))",  # @subject 없음
    "",                                        # 빈 문자열
    "(nonexistent_node_type) @subject",        # 없는 노드 이름
])
def test_a_bad_query_is_unmeasurable_not_a_pass(tmp_path, bad):
    assert cpp_query.compile_query(bad) is None
    result = verify(_repo(tmp_path), RuleCheck(
        kind="query", population_query=bad, conforming_query=_CONST_METHOD
    ))
    assert result is None      # 조용히 통과가 아니라 "잴 수 없음"


@requires_ts
def test_a_broken_conforming_query_is_not_blamed_on_the_repo(tmp_path):
    """대상은 다 찾았는데 만족하는 게 0개면, 리포가 자기 관행을 100% 어긴 게
    아니라 질의가 조건을 못 쓴 것이다 — 실측으로 모델이 정확히 이걸 했다."""
    inferred = [InferredRule(rule="인자 없는 메서드는 const 를 붙인다", check=RuleCheck(
        kind="query",
        population_query=_NO_ARG_METHOD,
        conforming_query='(field_declaration (function_declarator '
                         'declarator: (field_identifier) @subject (virtual_specifier) '
                         '(type_qualifier) (parameter_list) (parameter_list)))',
    ))]
    (_, reason), = verify_inferred(_repo(tmp_path), inferred).rejected
    assert "질의가 조건을 잘못 표현한" in reason
    assert "레포가 뒷받침하지 않음" not in reason


@requires_ts
def test_a_genuinely_wrong_rule_is_still_blamed_on_the_rule(tmp_path):
    """반대 방향 오진을 막는다: 손으로 쓴 검사의 0% 는 진짜로 규칙이 틀린 것."""
    (tmp_path / "a.h").write_text(
        "class C {\n" + "".join(f"  int m_v{i};\n" for i in range(20)) + "};\n",
        encoding="utf-8",
    )
    inferred = [InferredRule(rule="포인터 멤버는 Ptr 접미사를 쓴다", check=RuleCheck(
        kind="naming", category="member", facet="suffix", value="Ptr"))]
    (_, reason), = verify_inferred(tmp_path, inferred).rejected
    assert "레포가 뒷받침하지 않음" in reason


# ------------------------------------------------- 승인 후 리뷰가 적용하는가

@requires_ts
def test_review_flags_a_new_violation_of_an_approved_query_rule(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src" / "new.h").write_text(
        "class N {\n public:\n  int good() const;\n  int bad();\n};\n", encoding="utf-8"
    )
    rule = ConventionRule(
        id="ai-const-accessors", category="const",
        description="인자 없는 메서드는 const 를 붙인다", facet="other",
        coverage=0.95, occurrences=36, confidence="high",
        check=RuleCheck(kind="query",
                        population_query=_NO_ARG_METHOD,
                        conforming_query=_CONST_METHOD),
    )
    patch = """\
diff --git a/src/new.h b/src/new.h
index 1111111..2222222 100644
--- a/src/new.h
+++ b/src/new.h
@@ -1,4 +1,5 @@
 class N {
  public:
   int good() const;
+  int bad();
 };
"""
    scope = DiffScope(files=[FileDiff(
        path="src/new.h", patch_text=patch, added_ranges=[LineRange(start=4, end=4)]
    )])
    (finding,) = check_structural(scope, [rule], repo)
    assert "bad" in finding.title and finding.line == 4
    assert finding.evidence.reproducible is True     # 결정적 → CI 게이트 가능

    # 규칙을 지키는 줄만 건드리면 아무 말도 하지 않는다
    untouched = DiffScope(files=[FileDiff(
        path="src/new.h", patch_text=patch, added_ranges=[LineRange(start=3, end=3)]
    )])
    assert check_structural(untouched, [rule], repo) == []


@requires_ts
def test_the_llm_prompt_does_not_repeat_what_the_query_checker_caught():
    from pumpkins.llm.postprocess import _split_rules

    rule = ConventionRule(
        id="x", category="const", description="…", facet="other",
        coverage=1.0, occurrences=30, confidence="high",
        check=RuleCheck(kind="query", population_query=_NO_ARG_METHOD,
                        conforming_query=_CONST_METHOD),
    )
    machine, llm_judged = _split_rules([rule])
    assert machine == [rule] and llm_judged == []


# ----------------------------------------------------------- 어휘를 넘겨주는가

@requires_ts
def test_the_node_vocabulary_separates_named_from_anonymous(tmp_path):
    """이름 노드와 익명 노드를 갈라 보여주는 게 어휘 목록의 핵심이다 — 그 구분이
    질의가 조용히 0을 반환하는 가장 흔한 원인이라서."""
    macros = cpp_ast.NO_MACROS
    trees = [cpp_ast.parse_tree(_KLASS % 0, macros)]
    vocab = cpp_query.node_vocabulary(trees)
    assert "(field_declaration)" in vocab and "(function_declarator)" in vocab
    assert '"virtual"' in vocab and '"const"' in vocab
    # 연산자·구두점은 소음이라 넣지 않는다
    assert '"::"' not in vocab and '"("' not in vocab
