"""Tests for rule scoping — which files a convention rule is allowed to judge.

One repo rarely has one convention: legacy subtrees, generated code and headers
vs. translation units differ, and a rule applied outside the code it was
measured on is a false-positive machine.
"""

from pumpkins.conventions import ConventionRule, RuleScope, check_scope
from pumpkins.diff import parse_diff_text
from pumpkins.models import DiffScope


def _diff(path: str) -> DiffScope:
    """A one-file diff adding a member that breaks the `m_` prefix rule."""
    text = f"""\
diff --git a/{path} b/{path}
index 1111111..2222222 100644
--- a/{path}
+++ b/{path}
@@ -10,2 +10,3 @@ class Pool {{
 private:
     std::mutex m_mutex;
+    int count;
"""
    files, _ = parse_diff_text(text)
    return DiffScope(files=files)


def _rule(scope: RuleScope | None = None) -> ConventionRule:
    return ConventionRule(
        id="member-prefix-m_",
        category="member_variable",
        description="멤버 변수는 `m_` 접두사를 사용한다",
        facet="prefix",
        value="m_",
        coverage=0.92,
        occurrences=187,
        confidence="high",
        scope=scope or RuleScope(),
    )


# ------------------------------------------------------------ RuleScope units

def test_empty_scope_is_repo_wide():
    scope = RuleScope()
    assert scope.is_repo_wide
    assert scope.applies_to("anything/at/all.cpp")


def test_paths_narrow_to_a_subtree():
    scope = RuleScope(paths=["src/core"])
    assert scope.applies_to("src/core/pool.cpp")
    assert scope.applies_to("src/core/deep/nested/pool.cpp")
    assert not scope.applies_to("src/legacy/pool.cpp")


def test_exclude_paths_wins_over_paths():
    scope = RuleScope(paths=["src"], exclude_paths=["src/legacy"])
    assert scope.applies_to("src/core/pool.cpp")
    assert not scope.applies_to("src/legacy/pool.cpp")


def test_extensions_scope_headers_separately_from_sources():
    scope = RuleScope(extensions=["hpp", ".H"])  # normalized to .hpp/.h
    assert scope.extensions == [".hpp", ".h"]
    assert scope.applies_to("src/pool.hpp")
    assert scope.applies_to("src/pool.h")
    assert not scope.applies_to("src/pool.cpp")


def test_glob_patterns_match_generated_files():
    scope = RuleScope(exclude_paths=["*_generated.hpp"])
    assert not scope.applies_to("model/thing_generated.hpp")
    assert scope.applies_to("model/thing.hpp")


def test_matching_is_case_sensitive_for_reproducibility():
    """Same conventions.yml must give the same findings on every platform."""
    assert not RuleScope(paths=["SRC"]).applies_to("src/pool.cpp")


# ---------------------------------------------------- checker integration

def test_rule_applies_inside_its_scope():
    findings = check_scope(_diff("src/core/pool.h"), [_rule(RuleScope(paths=["src/core"]))])
    assert len(findings) == 1
    assert "`count`" in findings[0].title


def test_rule_stays_silent_outside_its_scope():
    findings = check_scope(_diff("src/legacy/pool.h"), [_rule(RuleScope(paths=["src/core"]))])
    assert findings == []


def test_excluded_subtree_keeps_its_own_conventions():
    rule = _rule(RuleScope(exclude_paths=["third_party", "src/legacy"]))
    assert check_scope(_diff("src/legacy/pool.h"), [rule]) == []
    assert len(check_scope(_diff("src/core/pool.h"), [rule])) == 1


def test_scope_is_named_in_the_finding():
    """The comment must say how far the rule reaches, or the reader cannot tell
    whether it even applies here."""
    rule = _rule(RuleScope(paths=["src/core"]))
    (finding,) = check_scope(_diff("src/core/pool.h"), [rule])
    assert "src/core" in finding.explanation


def test_single_word_name_never_violates_a_casing_rule():
    """`flush` is compatible with lowerCamel — flagging it would be a false
    positive produced purely by bucketing."""
    text = """\
diff --git a/src/pool.cpp b/src/pool.cpp
index 1111111..2222222 100644
--- a/src/pool.cpp
+++ b/src/pool.cpp
@@ -10,1 +10,3 @@
 void existing();
+void flush();
+void Do_Work();
"""
    files, _ = parse_diff_text(text)
    rule = ConventionRule(
        id="function-casing-lowerCamel",
        category="function",
        description="함수 이름은 lowerCamel 표기를 사용한다",
        facet="casing",
        value="lowerCamel",
        coverage=0.95,
        occurrences=412,
        confidence="high",
    )
    findings = check_scope(DiffScope(files=files), [rule])
    assert [f.title.split("`")[1] for f in findings] == ["Do_Work"]
