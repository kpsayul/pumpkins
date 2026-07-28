"""Structural (AST / tree-sitter) checks for `learn --infer`.

Real tree-sitter when installed (skipped otherwise); the graceful-degradation
path is tested without it by monkeypatching availability off."""

import pytest

from pumpkins.conventions import InferredRule, RuleCheck, verify, verify_inferred
from pumpkins.conventions import verifier as verifier_mod
from pumpkins.languages import cpp_ast

requires_ts = pytest.mark.skipif(
    not cpp_ast.available(), reason="tree-sitter (structural extra) not installed"
)


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
