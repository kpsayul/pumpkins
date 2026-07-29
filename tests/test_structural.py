"""Structural (AST / tree-sitter) checks for `learn --infer`.

Real tree-sitter when installed (skipped otherwise); the graceful-degradation
path is tested without it by monkeypatching availability off."""

import pytest

from pumpkins.conventions import (
    ConventionRule,
    InferredRule,
    RuleCheck,
    StoredRule,
    check_structural,
    verify,
    verify_inferred,
)
from pumpkins.conventions import verifier as verifier_mod
from pumpkins.languages import cpp_ast
from pumpkins.models import DiffScope, FileDiff, LineRange

requires_ts = pytest.mark.skipif(
    not cpp_ast.available(), reason="tree-sitter (structural extra) not installed"
)


def _return_type_rule() -> ConventionRule:
    return ConventionRule(
        id="ai-factory-unique-ptr", category="ownership",
        description="create*는 unique_ptr를 반환한다", facet="other",
        coverage=0.95, occurrences=20, confidence="high",
        check=RuleCheck(kind="return_type", name_prefix="create", type_contains="unique_ptr"),
    )


_FACTORY_SRC = (
    "#pragma once\n"
    "std::unique_ptr<A> createA();\n"   # line 2 — conforms
    "Widget* createBad();\n"            # line 3 — violates (raw pointer)
)


def _scope(added: list[tuple[int, int]]) -> DiffScope:
    return DiffScope(files=[FileDiff(
        path="src/factory.h",
        added_ranges=[LineRange(start=s, end=e) for s, e in added],
    )])


@requires_ts
def test_review_flags_a_structural_violation_on_a_changed_line(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "factory.h").write_text(_FACTORY_SRC, encoding="utf-8")

    findings = check_structural(_scope([(3, 3)]), [_return_type_rule()], tmp_path)  # createBad added
    assert len(findings) == 1
    assert findings[0].line == 3
    assert "createBad" in findings[0].title
    assert findings[0].evidence.detector.value == "convention"
    assert findings[0].evidence.reproducible is True   # deterministic → CI-safe


@requires_ts
def test_review_ignores_conforming_and_untouched_functions(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "factory.h").write_text(_FACTORY_SRC, encoding="utf-8")

    # only the conforming createA (line 2) is touched; the violating createBad is not
    assert check_structural(_scope([(2, 2)]), [_return_type_rule()], tmp_path) == []


@requires_ts
def test_structural_check_survives_disk_round_trip(tmp_path):
    (tmp_path / "a.h").write_text(
        "\n".join(f"std::unique_ptr<T{i}> create{i}();" for i in range(20)), encoding="utf-8"
    )
    inferred = [InferredRule(
        rule="create*는 unique_ptr 반환", kind="ownership",
        check=RuleCheck(kind="return_type", name_prefix="create", type_contains="unique_ptr"),
    )]
    rule = verify_inferred(tmp_path, inferred).verified[0]
    # a verified structural rule keeps its check, and it round-trips through the
    # stored (yaml) model so the review side can re-run it after approval.
    reloaded = StoredRule.model_validate(StoredRule(**rule.model_dump()).model_dump())
    assert reloaded.check.kind == "return_type"
    assert reloaded.check.type_contains == "unique_ptr"


def test_split_rules_treats_structural_check_as_machine_checked():
    from pumpkins.llm.postprocess import _split_rules

    struct = _return_type_rule()
    llm_only = ConventionRule(
        id="y", category="layout", description="에러는 코드로 반환", facet="other",
        coverage=1.0, occurrences=20, confidence="high",  # check kind=none
    )
    machine, llm_judged = _split_rules([struct, llm_only])
    assert struct in machine          # review checker handles it → not re-reported
    assert llm_only in llm_judged


@requires_ts
def test_cpp_ast_scan_categorizes_declarations():
    # the naming extractor now runs on this: templates/macros handled cleanly,
    # member visibility from the tree, constants apart from members.
    src = (
        "#define API inline\n"
        "class Widget {\npublic:\n  void doIt();\nprivate:\n  int m_x;\n"
        "  static constexpr int kMax = 8;\n};\n"
        "struct Bag { int m_y; };\n"
        "template <class T> struct Holder {};\n"
        "int freeFunc();\n"
    )
    got = set(cpp_ast.scan(src))
    assert {("class_type", "Widget"), ("class_type", "Bag"), ("class_type", "Holder")} <= got
    assert ("class_type", "T") not in got          # a template parameter is not a class
    assert {("function", "doIt"), ("function", "freeFunc")} <= got
    assert ("private_member", "m_x") in got
    assert ("public_field", "m_y") in got
    assert ("constant", "kMax") in got             # static constexpr, apart from members


@requires_ts
def test_cpp_ast_extracts_functions_and_return_types():
    src = (
        "std::unique_ptr<W> createW();\n"
        "W* makeThing() { return nullptr; }\n"
        "class F { std::shared_ptr<X> createX(); int m_n; };\n"
    )
    fns = {f.name: f.return_type for f in cpp_ast.functions(src)}
    assert "unique_ptr" in fns["createW"]
    assert "shared_ptr" in fns["createX"]
    assert fns["makeThing"] == "W"   # raw pointer is in the declarator, not the type
    assert "m_n" not in fns          # a member variable is not a function


@requires_ts
def test_verify_return_type_measures_real_coverage(tmp_path):
    # 2 of 3 create* functions return unique_ptr → 67% (structural: regex can't do this)
    (tmp_path / "a.h").write_text(
        "std::unique_ptr<A> createA();\n"
        "std::unique_ptr<B> createB();\n"
        "Raw* createRaw();\n",
        encoding="utf-8",
    )
    r = verify(tmp_path, RuleCheck(kind="return_type", name_prefix="create", type_contains="unique_ptr"))
    assert r.matches == 2 and r.total == 3


@requires_ts
def test_trailing_return_type_is_read(tmp_path):
    # modern C++: `auto f() -> unique_ptr<X>` — the real type is after ->, not `auto`
    fns = {f.name: f.return_type for f in cpp_ast.functions("auto createX() -> std::unique_ptr<X>;")}
    assert "unique_ptr" in fns["createX"]
    (tmp_path / "a.h").write_text(
        "auto createY() -> std::unique_ptr<Y>;\nauto createZ() -> Raw*;\n", encoding="utf-8"
    )
    r = verify(tmp_path, RuleCheck(kind="return_type", name_prefix="create", type_contains="unique_ptr"))
    assert r.matches == 1 and r.total == 2   # createY matches, createZ (raw) does not


@requires_ts
def test_declaration_and_definition_are_one_function(tmp_path):
    # a factory declared in a header and defined in a source is ONE function —
    # counting both would inflate the occurrence gate.
    (tmp_path / "f.h").write_text("std::unique_ptr<A> createA();\n", encoding="utf-8")
    (tmp_path / "f.cpp").write_text(
        "std::unique_ptr<A> createA() { return nullptr; }\n", encoding="utf-8"
    )
    r = verify(tmp_path, RuleCheck(kind="return_type", name_prefix="create", type_contains="unique_ptr"))
    assert r.matches == 1 and r.total == 1


@requires_ts
def test_verify_inferred_promotes_a_true_structural_rule(tmp_path):
    body = "\n".join(f"std::unique_ptr<T{i}> create{i}();" for i in range(20))
    (tmp_path / "a.h").write_text(body, encoding="utf-8")
    inferred = [
        InferredRule(
            rule="팩토리(create*)는 unique_ptr를 반환한다", kind="ownership",
            check=RuleCheck(kind="return_type", name_prefix="create", type_contains="unique_ptr"),
        )
    ]
    report = verify_inferred(tmp_path, inferred)
    assert len(report.verified) == 1 and not report.rejected
    rule = report.verified[0]
    # structural → facet=other (measured coverage, but no deterministic review checker yet)
    assert rule.facet == "other"
    assert rule.coverage == 1.0 and rule.occurrences == 20


@requires_ts
def test_verify_inferred_rejects_a_wrong_structural_guess(tmp_path):
    (tmp_path / "a.h").write_text(
        "\n".join(f"Raw* create{i}();" for i in range(20)), encoding="utf-8"
    )
    inferred = [
        InferredRule(
            rule="팩토리는 unique_ptr를 반환한다",
            check=RuleCheck(kind="return_type", name_prefix="create", type_contains="unique_ptr"),
        )
    ]
    report = verify_inferred(tmp_path, inferred)
    assert not report.verified and len(report.rejected) == 1


def test_structural_check_degrades_to_unverified_without_tree_sitter(tmp_path, monkeypatch):
    (tmp_path / "a.h").write_text("std::unique_ptr<A> createA();\n", encoding="utf-8")
    monkeypatch.setattr(verifier_mod.cpp_ast, "available", lambda: False)

    check = RuleCheck(kind="return_type", name_prefix="create", type_contains="unique_ptr")
    assert verify(tmp_path, check) is None   # cannot verify → not a silent pass

    inferred = [InferredRule(rule="팩토리는 unique_ptr를 반환한다", check=check)]
    report = verify_inferred(tmp_path, inferred)
    assert len(report.unverified) == 1 and not report.verified and not report.rejected
    assert report.unverified[0].facet == "other"
