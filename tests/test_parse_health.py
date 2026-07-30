"""매크로가 섞인 class 헤더를 읽는 법, 그리고 못 읽었을 때 그걸 말하는 법.

C++ 문법은 여기서 진짜로 모호하다. `class Foo bar;` 는 유효한 C++(클래스 타입
변수 선언)이고 `class FOO_API Bar;` 와 글자 모양이 같다. 텍스트만 봐서는 절대
구분할 수 없고, 리포가 무엇을 `#define` 했는지 알아야만 갈린다.

그래서 두 가지를 검증한다: (1) 추측 대신 리포에 물어보는가, (2) 그래도 판정
못 한 자리를 조용히 넘기지 않고 세어서 말하는가."""

import os

import pytest

from pumpkins.conventions.extractor import collect_macros, extract_stats_with_health
from pumpkins.languages.cpp import ast as cpp_ast
from pumpkins.languages.cpp import parser as cpp_parser

_ALLOW_MISSING = os.environ.get("PUMPKINS_ALLOW_NO_TREE_SITTER") == "1"
requires_ts = pytest.mark.skipif(
    not cpp_ast.available(), reason="PUMPKINS_ALLOW_NO_TREE_SITTER=1 (명시적 건너뛰기)"
)

# 리포가 `#define API` 를 선언했다는 뜻 (본문 없음 — 흔한 export 매크로 모양)
_MACROS = cpp_ast.MacroTable({"API": ""})


# ------------------------------------------------- 리포에 물어보기 (#define)

def test_only_object_like_macros_are_collected():
    """함수형 매크로는 클래스 이름 자리에 설 수 없으니 목록에서 뺀다."""
    text = (
        "#define API __declspec(dllexport)\n"
        "#define BARE\n"
        "#define VERSION 3\n"
        "#define MIN(a,b) ((a)<(b)?(a):(b))\n"
    )
    got = cpp_parser.object_like_macros(text)
    assert set(got) == {"API", "BARE", "VERSION"}
    # 이름만이 아니라 본문도 필요하다 — 걷어내는 대신 펼치기 때문
    assert got["API"] == "__declspec(dllexport)"
    assert got["BARE"] == "" and got["VERSION"] == "3"
    # `MIN(a,b)` 를 `MI` 로 잘라 읽던 정규식 backtracking 회귀 방지
    assert "MI" not in got and "MIN" not in got


def test_macros_are_collected_from_the_repo(tmp_path):
    (tmp_path / "dll.h").write_text("#define MYLIB_API\n", encoding="utf-8")
    (tmp_path / "a.cpp").write_text("int f();\n", encoding="utf-8")
    assert "MYLIB_API" in collect_macros(tmp_path)


# ------------------------------------------- 매크로 펼치기 (파일 전체에 적용)

def test_expansion_leaves_preprocessor_lines_alone():
    """헤더는 전부 `#ifndef GUARD` / `#define GUARD` 로 시작한다. 가드를 빈
    문자열로 펼치면 `#ifndef` 만 남아 오류가 된다 — 실측으로 97개 중 49개
    파일에서 없던 오류가 새로 생겼다."""
    table = cpp_ast.MacroTable({"FOO_H": "", "API": ""})
    src = "#ifndef FOO_H\n#define FOO_H\nclass API Foo {};\n#endif\n"
    out = src if not cpp_ast.available() else table.expand(src)
    assert "#ifndef FOO_H" in out and "#define FOO_H" in out
    assert "class  Foo {};" in out          # 코드 줄에서는 펼쳐졌다


def test_expansion_preserves_line_count():
    """줄 번호가 밀리면 구조 지적이 엉뚱한 코드를 가리킨다."""
    table = cpp_ast.MacroTable({"A": "__declspec(dllexport)", "B": ""})
    src = "A int x;\nB int y;\n\nA B int z;\n"
    assert table.expand(src).count("\n") == src.count("\n")


def test_a_macro_body_naming_another_macro_is_expanded():
    """`#define A  B C` — 한 번만 치환하면 B 가 남는다."""
    table = cpp_ast.MacroTable({"OUTER": "INNER", "INNER": ""})
    assert table.expand("OUTER int x;\n").strip() == "int x;"


def test_a_trailing_comment_is_not_pasted_into_the_use_site():
    """`#define X 1  // 설명` 의 본문에 주석을 넣으면 사용처의 뒷부분이
    전부 주석 처리된다."""
    got = cpp_parser.object_like_macros("#define X 1  // 왜 1인지\n")
    assert got["X"] == "1"


@requires_ts
def test_expanding_the_whole_file_beats_fixing_only_class_headers():
    """실측(yaml-cpp): 파일 전체 치환으로 파싱 오류 235 → 76, 그리고 통계에서
    쓰레기가 빠졌다 — `function 'string FpToString'` → `'FpToString'`,
    `public_field 'override'` ×14 → 사라짐.

    반환 타입이 이름에 붙어 들어오는 건 class 헤더만 고쳐서는 못 잡는다."""
    table = cpp_ast.MacroTable({"NOEX": "noexcept", "API": ""})
    src = 'class API Wrapper {\n public:\n  std::string dump() NOEX;\n};\n'
    names = {name for cat, name in cpp_ast.scan(src, table) if cat == "function"}
    assert "dump" in names
    assert not any(" " in n for n in names)   # `string dump` 같은 게 없어야 한다


# --------------------------------------------- 세 가지 모양, 각각 다르게 실패

@requires_ts
@pytest.mark.parametrize(
    "src, members_expected",
    [
        ("class API Foo : public Base { int m_x; };", ["m_x"]),   # 파서가 ERROR 를 낸다
        ("class API Foo { int m_x; };", ["m_x"]),                  # 오류 없이 조용히 틀린다
        ("struct API Foo { int m_x; };", ["m_x"]),
        ("class API Foo;", []),                                    # 유효한 C++ 과 구분 불가
    ],
)
def test_a_macro_in_a_class_header_resolves(src, members_expected):
    (cls,) = cpp_ast.classes(src, _MACROS)
    assert cls.name == "Foo"
    assert [m.name for m in cpp_ast.members(src, _MACROS)] == members_expected


@requires_ts
def test_valid_cpp_that_looks_identical_is_left_alone():
    """`class Foo bar;` 는 Foo 타입 변수 bar 선언 — 유효한 C++ 이다.
    매크로 목록에 Foo 가 없으므로 건드리면 안 된다."""
    (cls,) = cpp_ast.classes("class Foo bar;", _MACROS)
    assert cls.name == "Foo"      # bar 가 아니다


@requires_ts
def test_the_tree_proves_two_of_the_three_without_any_macro_list():
    """매크로 목록이 없어도 트리가 증명하는 건 고친다 — 이름 모양을 보고
    찍는 게 아니라, 유효한 C++ 로는 나올 수 없는 트리라서."""
    assert [c.name for c in cpp_ast.classes("class API Foo { int m_x; };")] == ["Foo"]
    assert [c.name for c in cpp_ast.classes("class API Foo : public B {};")] == ["Foo"]
    # 반면 `class API Foo;` 는 증명이 안 되므로 그냥 두고, 대신 센다 (아래 테스트)
    assert [c.name for c in cpp_ast.classes("class API Foo;")] == ["API"]


@requires_ts
def test_recovery_preserves_line_numbers():
    """구조 지적은 자기가 찾은 줄을 보고한다 — 줄이 밀리면 엉뚱한 코드를 가리킨다."""
    src = "\n\nclass API Foo : public Base {\n  int m_x;\n  Widget* m_w;\n};\n"
    (cls,) = cpp_ast.classes(src, _MACROS)
    assert cls.line == 3
    assert [(m.name, m.line) for m in cpp_ast.members(src, _MACROS)] == [
        ("m_x", 4), ("m_w", 5)
    ]


@requires_ts
def test_recovery_never_makes_a_parse_worse():
    """복구는 오류가 줄어들 때만 채택된다 — 이 불변식 덕분에 리포별 허용목록
    없이 모든 파일에 그냥 돌릴 수 있다."""
    for src in [
        "class Foo { int m_x; };",
        "int main() { return 0; }",
        "template <class T> struct H { T v; };",
        "class Foo bar;",
        ")))garbage((( class",
    ]:
        plain = cpp_ast._error_count(cpp_ast._parse(src, cpp_ast.NO_MACROS).root_node)
        with_macros = cpp_ast._error_count(cpp_ast._parse(src, _MACROS).root_node)
        assert with_macros <= plain, src


# --------------------------------------------------- 못 읽은 걸 말하는가

@requires_ts
def test_unresolved_headers_are_counted_not_swallowed():
    """판정 못 한 자리는 0건이 아니라 '모르겠다 N건'으로 남아야 한다."""
    src = "class UNKNOWN_MACRO Foo;\n"
    _, report = cpp_ast.scan_with_report(src, cpp_ast.NO_MACROS)
    assert report.unresolved == 1 and not report.clean
    # 매크로라고 알려주면 판정된다
    _, report2 = cpp_ast.scan_with_report(src, cpp_ast.MacroTable({"UNKNOWN_MACRO": ""}))
    assert report2.unresolved == 0


@requires_ts
def test_template_parse_errors_do_not_raise_an_alarm():
    """복잡한 템플릿의 파싱 오류는 진지한 C++ 리포마다 나오고 통계에는 영향이
    없다. 여기에 경고를 걸면 항상 켜져 있는 경고가 되고, 항상 켜진 경고는
    아무도 안 읽는다 — 정작 중요한 하나를 묻어버린다."""
    health = cpp_ast.ScanHealth()
    health.add("t.h", cpp_ast.ParseReport(error_nodes=141, unresolved=0))
    assert not health.needs_attention
    assert "통계에는 영향 없음" in health.summary()

    health.add("bad.h", cpp_ast.ParseReport(error_nodes=0, unresolved=3))
    assert health.needs_attention
    assert "멤버가 통계에서 빠졌을 수 있습니다" in health.summary()


@requires_ts
def test_worst_files_are_ranked_by_lost_data_not_by_noise():
    health = cpp_ast.ScanHealth()
    health.add("noisy_templates.h", cpp_ast.ParseReport(error_nodes=141, unresolved=0))
    health.add("lost_a_class.h", cpp_ast.ParseReport(error_nodes=2, unresolved=1))
    health.finalize()
    assert health.worst[0][0] == "lost_a_class.h"


@requires_ts
def test_a_scan_reports_health_alongside_the_statistics(tmp_path):
    """실측 근거: yaml-cpp 에서 이 복구를 켜자 판정불가 class 헤더 25 → 0,
    private_member 92 → 124 였다. 읽기가 틀리면 통계가 틀린다."""
    (tmp_path / "dll.h").write_text("#define MYLIB_API\n", encoding="utf-8")
    (tmp_path / "w.h").write_text(
        "class MYLIB_API Widget : public Base {\n private:\n  int m_x;\n  int m_y;\n};\n",
        encoding="utf-8",
    )
    stats, health = extract_stats_with_health(tmp_path)
    members = next(s for s in stats if s.category == "private_member")
    assert members.total == 2                 # 복구 없이는 0
    assert health.files == 2 and not health.needs_attention
