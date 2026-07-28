"""Tests for the deterministic convention checker (diff vs conventions.yml).
No LLM involved — this whole stage must work without an API key."""

from pathlib import Path

from pumpkins.conventions import (
    ConventionRule,
    StoredRule,
    check_scope,
    load_conventions,
    write_rule,
)
from pumpkins.conventions.extractor import extract_stats
from pumpkins.diff import parse_diff_text
from pumpkins.models import DetectorKind, DiffScope


def parse_diff_text_files(diff: str):
    """Just the C++ FileDiffs — the skipped-path half is covered separately."""
    files, _ = parse_diff_text(diff)
    return files

# Hunk 1 has class scaffolding (private:) → member matching enabled.
# Hunk 2 has none → `int count;` there would be a local, not checked.
DIFF = """\
diff --git a/src/pool.h b/src/pool.h
index 1111111..2222222 100644
--- a/src/pool.h
+++ b/src/pool.h
@@ -10,2 +10,5 @@ class ThreadPool {
 private:
     std::mutex m_mutex;
+    int count;
+    int m_total;
+    bool m_running;
@@ -30,1 +33,4 @@
 void runLoop();
+void submitTask(int id);
+void Do_Work(int id);
+    int count;
"""

RULES = [
    ConventionRule(
        id="member-prefix-m_",
        category="member_variable",
        description="멤버 변수는 `m_` 접두사를 사용한다",
        facet="prefix",
        value="m_",
        coverage=0.92,
        occurrences=187,
        confidence="high",
    ),
    ConventionRule(
        id="function-casing-lowerCamel",
        category="function",
        description="함수 이름은 lowerCamel 표기를 사용한다",
        facet="casing",
        value="lowerCamel",
        coverage=0.95,
        occurrences=412,
        confidence="high",
    ),
    ConventionRule(  # facet=other must be ignored by the checker
        id="interface-prefix-I",
        category="class_type",
        description="인터페이스 클래스는 I 접두사를 사용한다",
        facet="other",
        value="samples show IWorker, IQueue",
        coverage=0.9,
        occurrences=30,
        confidence="medium",
    ),
]


def _scope() -> DiffScope:
    return DiffScope(base_ref="main", files=parse_diff_text_files(DIFF))


def test_checker_flags_violations_only():
    findings = check_scope(_scope(), RULES)
    by_rule = {f.evidence.rule_id: f for f in findings}

    # `count` in the class hunk violates the member prefix rule
    member = by_rule["member-prefix-m_"]
    assert "`count`" in member.title
    assert member.evidence.detector is DetectorKind.convention
    assert "m_count" in member.suggestion
    # question-form, evidence-backed explanation
    assert "?" in member.explanation and "92%" in member.explanation
    # ...and the same numbers structured, not only in the prose
    assert (member.evidence.occurrences, member.evidence.coverage) == (187, 0.92)
    assert member.evidence.reproducible is True

    # `Do_Work` violates the function casing rule; submitTask conforms
    func = by_rule["function-casing-lowerCamel"]
    assert "`Do_Work`" in func.title
    assert func.suggestion == ""  # casing renames are left to the reviewer

    # exactly these two: m_total/m_running/submitTask conform, the second
    # `int count;` is outside class context, facet=other rule is skipped
    assert len(findings) == 2


def test_member_context_from_hunk_section_header():
    """Additions deep inside a class body have no scaffolding in the hunk
    lines — the class context arrives via git's hunk section header."""
    diff = """\
diff --git a/src/pool.h b/src/pool.h
index 1111111..2222222 100644
--- a/src/pool.h
+++ b/src/pool.h
@@ -12,2 +12,3 @@ private:
     int m_capacity;
     bool m_running;
+    int counter;
"""
    scope = DiffScope(files=parse_diff_text_files(diff))
    findings = check_scope(scope, RULES)
    assert len(findings) == 1
    assert "`counter`" in findings[0].title


def test_checker_without_checkable_rules():
    other_only = [r for r in RULES if r.facet == "other"]
    assert check_scope(_scope(), other_only) == []


def test_store_roundtrip(tmp_path):
    """A rule written to conventions/rules/ comes back checkable."""
    root = tmp_path / "conventions"
    for rule in RULES:
        write_rule(root, "active", StoredRule(**rule.model_dump()))

    loaded = load_conventions(root)
    assert {r.id for r in loaded} == {r.id for r in RULES}
    by_id = {r.id for r in loaded}
    assert "member-prefix-m_" in by_id
    assert next(r for r in loaded if r.id == "member-prefix-m_").value == "m_"


def test_legacy_single_file_still_loads(tmp_path):
    """Repos that adopted pumpkins before the directory layout must keep working —
    the old format is still read, just never written."""
    path = tmp_path / "conventions.yml"
    path.write_text(
        "version: 1\n"
        "rules:\n"
        "  - id: member-prefix-m_\n"
        "    category: member_variable\n"
        "    description: 멤버 변수는 m_ 접두사를 사용한다\n"
        "    facet: prefix\n"
        "    value: m_\n"
        "    coverage: 0.92\n"
        "    occurrences: 187\n"
        "    confidence: high\n",
        encoding="utf-8",
    )
    (loaded,) = load_conventions(path)
    assert loaded.id == "member-prefix-m_" and loaded.value == "m_"
    assert loaded.scope.is_repo_wide  # absent scope defaults to repo-wide


def test_candidates_are_not_enforced(tmp_path):
    """Approval must be a real gate: a pending candidate has no effect on review."""
    root = tmp_path / "conventions"
    write_rule(root, "candidate", StoredRule(**RULES[0].model_dump()))
    assert load_conventions(root) == []

    write_rule(root, "active", StoredRule(**RULES[0].model_dump()))
    assert [r.id for r in load_conventions(root)] == [RULES[0].id]


def test_learn_stats_feed_checkable_rules(tmp_path):
    """The learn-side facets and the check-side facets must speak the same
    vocabulary — a rule built from extract_stats keys must be checkable."""
    (tmp_path / "a.h").write_text(
        "class Pool {\nprivate:\n    int m_size;\n};\n", encoding="utf-8"
    )
    stats = {s.category: s for s in extract_stats(tmp_path)}
    members = stats["private_member"]
    dominant_prefix = max(members.prefix_counts, key=members.prefix_counts.get)
    assert dominant_prefix == "m_"  # exactly the string a rule's `value` would carry


# ----------------------------------------- 카테고리 하위 호환 (접근 지정자 도입 후)

def test_old_generic_member_rule_still_applies():
    """멤버가 visibility로 갈리기 전에 쓰인 규칙이 계속 동작해야 한다 — 안 그러면
    이미 승인해 커밋한 규칙이 조용히 죽는다."""
    diff = """\
diff --git a/src/pool.h b/src/pool.h
index 1111111..2222222 100644
--- a/src/pool.h
+++ b/src/pool.h
@@ -10,2 +10,3 @@ class ThreadPool {
 private:
     std::mutex m_mutex;
+    int count;
"""
    scope = DiffScope(files=parse_diff_text_files(diff))
    (finding,) = check_scope(scope, [RULES[0]])   # category: member_variable
    assert "`count`" in finding.title


def test_a_private_member_rule_respects_the_visibility_in_the_hunk():
    """visibility로 갈린 규칙은 해당 구역에서만 동작해야 한다. spdlog에서 private은
    `_` 100%, public은 13%였으니 public 필드에 이 규칙을 들이대면 전부 오탐이 된다."""
    private_rule = RULES[0].model_copy(
        update={"category": "private_member", "facet": "suffix", "value": "_"}
    )

    def scope_for(specifier: str) -> DiffScope:
        diff = f"""\
diff --git a/src/pool.h b/src/pool.h
index 1111111..2222222 100644
--- a/src/pool.h
+++ b/src/pool.h
@@ -10,1 +10,2 @@ class ThreadPool {{
 {specifier}
+    int count;
"""
        return DiffScope(files=parse_diff_text_files(diff))

    # private 구역의 접미사 없는 멤버 → 규칙 위반
    (finding,) = check_scope(scope_for("private:"), [private_rule])
    assert "`count`" in finding.title
    # 같은 줄이 public 구역이면 이 규칙의 대상이 아니다
    assert check_scope(scope_for("public:"), [private_rule]) == []
