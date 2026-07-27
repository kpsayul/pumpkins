"""Tests for the conventions/ rule store and the learn re-run reconciliation.

The defect these guard against: `learn` used to overwrite conventions.yml
wholesale, so curating the file was punished — delete a wrong rule, re-run
learn, the rule came back. Re-running must never cost the user a decision.
"""

from pathlib import Path

import pytest

from pumpkins.conventions import (
    ConventionRule,
    RuleScope,
    StoredRule,
    apply,
    count_candidates,
    load_active_rules,
    load_all,
    load_status,
    reconcile,
    rule_filename,
    write_rule,
)


def _rule(
    rule_id: str = "member-prefix-m",
    category: str = "member_variable",
    facet: str = "prefix",
    value: str = "m",
    coverage: float = 0.98,
    occurrences: int = 1842,
) -> ConventionRule:
    return ConventionRule(
        id=rule_id,
        category=category,
        description=f"{category}는 {value} 규칙을 따른다",
        facet=facet,
        value=value,
        coverage=coverage,
        occurrences=occurrences,
        confidence="high",
    )


def _stored(rule: ConventionRule, reason: str = "") -> StoredRule:
    return StoredRule(**rule.model_dump(), reason=reason)


def _existing(active=(), candidate=(), archived=()):
    return {
        "active": {r.id: r for r in active},
        "candidate": {r.id: r for r in candidate},
        "archived": {r.id: r for r in archived},
    }


# ------------------------------------------------------------------ filenames

@pytest.mark.parametrize(
    "rule_id, expected",
    [
        ("member-prefix-m_", "member-prefix-m_.yml"),
        ("cpp.naming.member-prefix", "cpp.naming.member-prefix.yml"),
        ("weird/id with spaces", "weird-id-with-spaces.yml"),
        ("...", "unnamed-rule.yml"),
    ],
)
def test_rule_ids_become_safe_filenames(rule_id, expected):
    """Ids come from an LLM, so they cannot be trusted as paths."""
    assert rule_filename(rule_id) == expected


# ---------------------------------------------------------- status = directory

def test_only_active_rules_are_enforced(tmp_path):
    root = tmp_path / "conventions"
    write_rule(root, "active", _stored(_rule("a")))
    write_rule(root, "candidate", _stored(_rule("b")))
    write_rule(root, "archived", _stored(_rule("c")))

    assert [r.id for r in load_active_rules(root)] == ["a"]
    assert count_candidates(root) == 1


def test_written_rule_survives_a_roundtrip(tmp_path):
    root = tmp_path / "conventions"
    original = _stored(
        _rule(), reason="팀 합의 2026-07: 신규 코드에 적용"
    )
    original.scope = RuleScope(exclude_paths=["src/legacy"])
    write_rule(root, "active", original)

    (loaded,) = load_status(root, "active").values()
    assert loaded.reason == original.reason
    assert loaded.scope.exclude_paths == ["src/legacy"]
    assert loaded.value == "m"


def test_rule_file_tells_the_reader_how_to_change_its_status(tmp_path):
    root = tmp_path / "conventions"
    path = write_rule(root, "candidate", _stored(_rule()))
    text = path.read_text(encoding="utf-8")
    assert "git mv" in text          # 승인/기각 방법
    assert "git log --follow" in text  # 이력은 git에 있다


# -------------------------------------------------------------- reconciliation

def test_new_rule_becomes_a_candidate():
    rec = reconcile(_existing(), [_rule()])
    assert [r.id for r in rec.new_candidates] == ["member-prefix-m"]
    assert rec.needs_attention


def test_unchanged_rule_is_left_alone():
    rule = _rule()
    rec = reconcile(_existing(active=[_stored(rule)]), [rule])
    assert rec.unchanged == [rule.id]
    assert rec.new_candidates == [] and rec.refreshed == []


def test_drifting_statistics_refresh_without_reopening_the_decision():
    """A repo growing is not a new decision — the human's reason must survive."""
    approved = _stored(_rule(occurrences=1842), reason="테크리드 승인")
    grown = _rule(occurrences=5200, coverage=0.99)

    rec = reconcile(_existing(active=[approved]), [grown])

    assert [r.id for r in rec.refreshed] == [grown.id]
    assert rec.refreshed[0].occurrences == 5200
    assert rec.refreshed[0].reason == "테크리드 승인"
    assert rec.new_candidates == []


def test_rejected_rule_is_not_proposed_again():
    """A rejection is a decision, not an absence. Re-nagging is how a
    human-in-the-loop tool gets ignored."""
    rejected = _stored(_rule("member-prefix-m_", value="m_"), reason="오탐이 많아 기각")
    rec = reconcile(_existing(archived=[rejected]), [_rule("member-prefix-m_", value="m_")])

    assert rec.suppressed == ["member-prefix-m_"]
    assert rec.new_candidates == []


def test_reconsider_reopens_an_archived_rule():
    rejected = _stored(_rule("member-prefix-m_", value="m_"))
    rec = reconcile(
        _existing(archived=[rejected]),
        [_rule("member-prefix-m_", value="m_")],
        reconsider=True,
    )
    assert [r.id for r in rec.new_candidates] == ["member-prefix-m_"]
    assert rec.suppressed == []


def test_changed_value_is_a_replacement_proposal_not_a_silent_flip():
    """Repo migrated m_ → m. The active rule must stay active until a human
    decides; the new value arrives as a candidate that names what it replaces."""
    active = _stored(_rule("member-prefix-m_", value="m_"), reason="2025 합의")
    rec = reconcile(_existing(active=[active]), [_rule("member-prefix-m", value="m")])

    assert rec.superseded == [("member-prefix-m_", "member-prefix-m")]
    (candidate,) = rec.new_candidates
    assert candidate.id == "member-prefix-m"
    assert "member-prefix-m_" in candidate.reason  # 무엇을 대체하는지 파일에 적힌다
    assert rec.needs_attention


def test_rule_the_scan_no_longer_supports_is_flagged_not_deleted():
    active = _stored(_rule("function-casing-lowerCamel", category="function", facet="casing",
                           value="lowerCamel"))
    rec = reconcile(_existing(active=[active]), [])

    assert rec.stale == ["function-casing-lowerCamel"]
    assert rec.new_candidates == []  # 삭제도, 대체도 하지 않는다


def test_learned_metadata_is_recorded_on_proposals():
    rec = reconcile(_existing(), [_rule()], model="gpt-4o-mini")
    (candidate,) = rec.new_candidates
    assert candidate.model == "gpt-4o-mini"
    assert candidate.learned_at is not None


# ------------------------------------------------------------------- applying

def test_apply_writes_candidates_by_default(tmp_path):
    root = tmp_path / "conventions"
    rec = reconcile(_existing(), [_rule()])
    apply(root, rec)

    assert load_active_rules(root) == []          # 승인 전엔 적용 안 됨
    assert count_candidates(root) == 1


def test_accept_all_writes_straight_to_active(tmp_path):
    root = tmp_path / "conventions"
    rec = reconcile(_existing(), [_rule()])
    apply(root, rec, accept_all=True)

    assert [r.id for r in load_active_rules(root)] == ["member-prefix-m"]
    assert count_candidates(root) == 0


def test_rerun_does_not_resurrect_a_rule_the_user_archived(tmp_path):
    """The end-to-end shape of the original defect, at the store level."""
    root = tmp_path / "conventions"
    rule = _rule()

    apply(root, reconcile(load_all(root), [rule]), accept_all=True)
    assert [r.id for r in load_active_rules(root)] == [rule.id]

    # 사용자가 규칙을 기각으로 옮긴다 (git mv에 해당)
    active_file = next((root / "rules").glob("*.yml"))
    archived = root / "archive"
    archived.mkdir()
    active_file.rename(archived / active_file.name)

    # learn을 다시 돌려도 되살아나지 않는다
    rec = reconcile(load_all(root), [rule])
    apply(root, rec)
    assert load_active_rules(root) == []
    assert count_candidates(root) == 0
    assert rec.suppressed == [rule.id]
