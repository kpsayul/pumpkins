"""verification/learn_scoring.py의 결정적 부분에 대한 테스트 (API 키 불필요).

채점 로직·스케일·관측 커버리지·비용 추정은 LLM 없이 검증 가능한 축이라
여기서 잠근다. learn_with_usage처럼 네트워크가 필요한 것은 제외 — 그건
run_verification.py / score_real_repos.py가 키가 있을 때 실측한다.

learn_scoring은 verification/ 아래 flat 모듈이라 sys.path에 얹어서 import한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "verification"))

import learn_scoring as ls  # noqa: E402


# ---- 채점용 가짜 규칙 (ConventionRule 인터페이스 중 채점이 쓰는 필드만) --------

class FakeRule:
    def __init__(self, category, facet, value):
        self.category = category
        self.facet = facet
        self.value = value


def _expected(*triples):
    return [ls.ExpectedRule(c, f, v, source="test") for c, f, v in triples]


# ------------------------------------------------------------------ score_rules

def test_score_perfect_match():
    expected = _expected(
        ("member_variable", "prefix", "m_"),
        ("function", "casing", "lowerCamel"),
    )
    adopted = [FakeRule("member_variable", "prefix", "m_"),
               FakeRule("function", "casing", "lowerCamel")]
    score = ls.score_rules(adopted, expected)
    assert score.recall == 1.0
    assert score.precision == 1.0
    assert score.missed == []
    assert score.wrong == []


def test_score_missing_rule_lowers_recall_not_precision():
    expected = _expected(
        ("function", "casing", "UpperCamel"),
        ("class_type", "casing", "UpperCamel"),
        ("member_variable", "suffix", "_"),
    )
    # 멤버 트레일링 _ 는 게이트에 걸려 채택 안 됨(=googletest 실제 상황)
    adopted = [FakeRule("function", "casing", "UpperCamel"),
               FakeRule("class_type", "casing", "UpperCamel")]
    score = ls.score_rules(adopted, expected)
    assert score.recall == 2 / 3
    assert score.precision == 1.0  # 채택한 둘은 정확 — 없는 걸 틀렸다 하지 않음
    assert ("member_variable", "suffix", "_") in score.missed


def test_score_wrong_value_on_judged_axis_lowers_precision():
    expected = _expected(("function", "casing", "lowerCamel"))
    adopted = [FakeRule("function", "casing", "UpperCamel")]  # 같은 축, 다른 값
    score = ls.score_rules(adopted, expected)
    assert score.recall == 0.0
    assert score.precision == 0.0
    assert ("function", "casing", "UpperCamel") in score.wrong


def test_score_extra_rule_off_axis_is_ignored():
    # 정답이 판정하지 않는 축(constant/casing)의 추가 채택은 precision을 깎지 않음
    expected = _expected(("function", "casing", "lowerCamel"))
    adopted = [FakeRule("function", "casing", "lowerCamel"),
               FakeRule("constant", "casing", "UPPER_SNAKE")]
    score = ls.score_rules(adopted, expected)
    assert score.recall == 1.0
    assert score.precision == 1.0
    assert score.wrong == []


def test_score_facet_other_is_not_counted():
    # facet=other 규칙은 자동 검증 불가 — 채택 집합에서 빠진다
    expected = _expected(("function", "casing", "lowerCamel"))
    adopted = [FakeRule("function", "casing", "lowerCamel"),
               FakeRule("class_type", "other", "I-prefix interfaces")]
    score = ls.score_rules(adopted, expected)
    assert ("class_type", "other", "I-prefix interfaces") not in score.adopted
    assert score.precision == 1.0


def test_score_nothing_adopted():
    expected = _expected(("member_variable", "prefix", "m_"))
    score = ls.score_rules([], expected)
    assert score.recall == 0.0
    assert score.precision == 1.0  # 오채택이 없으니 precision은 1 (Catch2형 상황)


# ------------------------------------------------------------- CategoryStats 대역

class FakeStats:
    """extract_stats가 내는 CategoryStats 중 스케일/커버리지가 읽는 필드만."""

    def __init__(self, category, total, prefix=None, suffix=None, casing=None,
                 casing_informative=None, casing_ambiguous=0):
        self.category = category
        self.total = total
        self.prefix_counts = prefix or {}
        self.suffix_counts = suffix or {}
        self.casing_counts = casing or {}
        self.casing_informative = total if casing_informative is None else casing_informative
        self.casing_ambiguous = casing_ambiguous
        self.samples = []

    def model_copy(self, deep=True):
        return FakeStats(
            self.category, self.total, dict(self.prefix_counts),
            dict(self.suffix_counts), dict(self.casing_counts),
            self.casing_informative, self.casing_ambiguous,
        )


# ------------------------------------------------------------- scale / coverage

def test_scale_to_gate_multiplies_and_preserves_distribution():
    stats = [
        FakeStats("member_variable", 6, prefix={"m_": 6}),
        FakeStats("class_type", 2, casing={"UpperCamel": 2}, casing_informative=2),
    ]
    scaled, factor = ls.scale_to_gate(stats, 20)
    # 가장 작은 total(2)을 20 이상으로: ceil(20/2)=10
    assert factor == 10
    by_cat = {s.category: s for s in scaled}
    assert by_cat["member_variable"].total == 60
    assert by_cat["member_variable"].prefix_counts["m_"] == 60  # 분포(100%) 보존
    assert by_cat["class_type"].total == 20
    assert by_cat["class_type"].casing_informative == 20


def test_scale_to_gate_noop_when_already_above():
    stats = [FakeStats("function", 40, casing={"lowerCamel": 40}, casing_informative=40)]
    scaled, factor = ls.scale_to_gate(stats, 20)
    assert factor == 1
    assert scaled[0].total == 40


def test_scale_to_gate_empty():
    scaled, factor = ls.scale_to_gate([], 20)
    assert factor == 1
    assert scaled == []


def test_observed_coverage_casing_uses_informative_denominator():
    stats = [FakeStats("function", 100, casing={"UpperCamel": 88, "lower_snake": 10},
                       casing_informative=98)]
    count, denom, frac = ls.observed_coverage(stats, "function", "casing", "UpperCamel")
    assert count == 88
    assert denom == 98  # total(100)이 아니라 casing_informative
    assert abs(frac - 88 / 98) < 1e-9


def test_observed_coverage_prefix_uses_total_denominator():
    stats = [FakeStats("member_variable", 121, suffix={"_": 86})]
    count, denom, frac = ls.observed_coverage(stats, "member_variable", "suffix", "_")
    assert count == 86
    assert denom == 121
    assert abs(frac - 86 / 121) < 1e-9


def test_observed_coverage_missing_category():
    assert ls.observed_coverage([], "function", "casing", "lowerCamel") == (0, 0, 0.0)


# ------------------------------------------------------------------- cost

def test_estimate_cost_known_model():
    # gpt-4o-mini: (0.15, 0.60) / 1M — 1e6 in + 1e6 out = 0.15 + 0.60
    cost = ls.estimate_cost_usd("gpt-4o-mini", 1_000_000, 1_000_000)
    assert abs(cost - 0.75) < 1e-9


def test_estimate_cost_unknown_model_is_none():
    assert ls.estimate_cost_usd("some-unlisted-model", 1000, 1000) is None


def test_costlog_accumulates_tokens_and_usd():
    log = ls.CostLog()
    log.add("a", ls.LearnRun("gpt-4o-mini", [], 1_000_000, 1_000_000))
    log.add("b", ls.LearnRun("gpt-4o-mini", [], 2_000_000, 0))
    assert log.total_tokens == 4_000_000
    # a=0.75, b=2M in * 0.15/1M = 0.30 → 1.05
    assert abs(log.total_usd - 1.05) < 1e-9
    md = log.as_markdown()
    assert any("합계" in line for line in md)


def test_costlog_total_usd_none_when_all_unknown():
    log = ls.CostLog()
    log.add("x", ls.LearnRun("unlisted", [], 100, 100))
    assert log.total_usd is None


def test_learnrun_totals_and_cost():
    run = ls.LearnRun("gpt-4o", [], 1_000_000, 0)
    assert run.total_tokens == 1_000_000
    assert abs(run.cost_usd - 2.50) < 1e-9  # gpt-4o input 2.50/1M
