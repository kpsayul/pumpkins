"""Tests for check profiles and the prompt they assemble.

A profile decides what the review looks for. It used to be half a dict of
clang-tidy checks and half a hardcoded concurrency system prompt, which is how a
real PR came back with an empty verdict: its defect was a C++17 construct in a
C++11 library, and nothing in the prompt asked about that.
"""

import pytest

from pumpkins.analysis.checks import CHECK_PROFILES, checks_arg
from pumpkins.conventions import ConventionRule, RuleScope
from pumpkins.llm.postprocess import build_system_prompt
from pumpkins.profiles import DEFAULT_PROFILE, PROFILES, get_profile


def _rule(rule_id: str, facet: str, description: str, scope=None) -> ConventionRule:
    return ConventionRule(
        id=rule_id,
        category="member_variable",
        description=description,
        facet=facet,
        value="m_" if facet != "other" else "samples show IWorker",
        coverage=0.92,
        occurrences=187,
        confidence="high",
        scope=scope or RuleScope(),
    )


class _Standard:
    minimum = 11
    sources = ["CMakeLists.txt"]


# ------------------------------------------------------------------ profiles

def test_a_profile_defines_both_halves_in_one_place():
    """Splitting them across modules is how the LLM half went stale."""
    for name, profile in PROFILES.items():
        assert profile.name == name
        assert profile.llm_focus.strip(), f"{name} has no LLM instructions"
        assert profile.description.strip()


def test_check_profiles_stay_derived_from_the_profile_registry():
    assert set(CHECK_PROFILES) == set(PROFILES)
    assert CHECK_PROFILES["concurrency"] == PROFILES["concurrency"].clang_tidy_checks


def test_checks_arg_disables_everything_else_first():
    arg = checks_arg("concurrency")
    assert arg.startswith("-*,")
    assert "concurrency-*" in arg


def test_unknown_profile_is_rejected_by_name():
    with pytest.raises(KeyError, match="unknown check profile"):
        get_profile("nope")


def test_default_profile_exists():
    assert DEFAULT_PROFILE in PROFILES


# ------------------------------------------------------------- prompt assembly

def test_prompt_carries_the_active_profile_and_not_the_others():
    prompt = build_system_prompt(get_profile("portability"))
    assert "inline variables at namespace scope" in prompt  # portability focus
    assert "lock acquisition order" not in prompt           # concurrency focus


def test_portability_prompt_states_the_declared_standard():
    prompt = build_system_prompt(get_profile("portability"), cxx_standard=_Standard())
    assert "C++11" in prompt
    assert "CMakeLists.txt" in prompt


def test_portability_prompt_refuses_to_guess_a_standard():
    """A wrong minimum turns every modern construct into a false positive."""
    prompt = build_system_prompt(get_profile("portability"), cxx_standard=None)
    assert "NOT DECLARED" in prompt
    assert "Do not guess" in prompt


def test_other_profiles_do_not_mention_the_standard():
    prompt = build_system_prompt(get_profile("concurrency"), cxx_standard=None)
    assert "minimum C++ standard" not in prompt


# ------------------------------------------------------------ rules in prompt

def test_machine_checked_rules_are_listed_but_marked_do_not_report():
    """Otherwise the model re-reports every naming violation the regex checker
    just reported, and the user sees each finding twice."""
    prompt = build_system_prompt(
        get_profile("concurrency"),
        rules=[_rule("member-prefix-m_", "prefix", "멤버 변수는 m_ 접두사")],
    )
    assert "member-prefix-m_" in prompt
    assert "do NOT report these" in prompt


def test_rules_a_regex_cannot_express_are_handed_to_the_model():
    """facet: other rules were written to the file and then skipped by everyone.
    This is the path that makes them do something."""
    prompt = build_system_prompt(
        get_profile("concurrency"),
        rules=[_rule("interface-prefix-I", "other", "인터페이스는 I 접두사")],
    )
    assert "interface-prefix-I" in prompt
    assert "Only you can check these" in prompt


def test_rule_scope_is_shown_so_the_model_does_not_overreach():
    prompt = build_system_prompt(
        get_profile("concurrency"),
        rules=[
            _rule(
                "legacy-exempt", "other", "레거시 예외",
                scope=RuleScope(exclude_paths=["src/legacy"]),
            )
        ],
    )
    assert "src/legacy" in prompt


def test_no_rules_section_without_rules():
    assert "Repository rules" not in build_system_prompt(get_profile("concurrency"))


# ------------------------------------------------- 근거로 쓸 수 있는 규칙의 범위

def test_llm_cannot_cite_a_rule_nobody_approved():
    """A model asked for an id will sometimes invent a plausible one. Accepting
    it would make the provenance label lie about what backs the finding."""
    from pumpkins.llm.postprocess import LlmPostProcessor

    processor = LlmPostProcessor.__new__(LlmPostProcessor)  # no SDK needed
    processor.model = "test-model"
    known = {"member-prefix-m_"}

    assert processor._verified_rule_id("member-prefix-m_", known) == "member-prefix-m_"
    assert processor._verified_rule_id("rule-that-does-not-exist", known) is None
    assert processor._verified_rule_id(None, known) is None
    assert processor._verified_rule_id("", known) is None
