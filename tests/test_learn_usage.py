"""ConventionLearner.learn_with_usage() 가 토큰 사용량을 반환하는지 (키 불필요).

learn()은 규칙만 돌려주는 하위호환 API로 남고, 사용량이 필요한 호출자(검증 하네스
등)는 learn_with_usage()로 usage를 함께 받는다 — 예전엔 usage가 로그로만 남아
클라이언트를 직접 호출해야 했던 공백을 메운 것.
"""

from pumpkins.conventions import ConventionLearner, LearnOutcome, LearnResult
from pumpkins.llm.provider import ParsedResult


class _RecordingClient:
    def __init__(self, parsed, in_tok, out_tok):
        self._result = ParsedResult(parsed=parsed, input_tokens=in_tok, output_tokens=out_tok)
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


def _learner(monkeypatch, parsed, in_tok=123, out_tok=45):
    from pumpkins.conventions import learner

    client = _RecordingClient(parsed, in_tok, out_tok)
    monkeypatch.setattr(learner, "get_client", lambda: client)
    return ConventionLearner(model="test-model"), client


def test_learn_with_usage_returns_tokens(monkeypatch):
    learner_obj, _ = _learner(monkeypatch, LearnResult(rules=[]), in_tok=1512, out_tok=203)
    outcome = learner_obj.learn_with_usage([])
    assert isinstance(outcome, LearnOutcome)
    assert isinstance(outcome.result, LearnResult)
    assert outcome.input_tokens == 1512
    assert outcome.output_tokens == 203
    assert outcome.total_tokens == 1512 + 203


def test_learn_still_returns_bare_result_backward_compatible(monkeypatch):
    # 하위호환: learn()은 여전히 LearnResult를 그대로 돌려준다 (usage 없이)
    learner_obj, _ = _learner(monkeypatch, LearnResult(rules=[]))
    result = learner_obj.learn([])
    assert isinstance(result, LearnResult)


def test_learn_with_usage_applies_threshold_gate(monkeypatch):
    # 게이트가 learn_with_usage 안에서 적용되는지 — 미달 규칙은 result에서 빠진다
    from pumpkins.conventions.learner import ConventionRule

    weak = ConventionRule(
        id="member-prefix-m_", category="member_variable",
        description="d", facet="prefix", value="m_",
        coverage=0.50, occurrences=5, confidence="low",  # occ<20, cov<85% → 기각
    )
    learner_obj, _ = _learner(monkeypatch, LearnResult(rules=[weak]))
    outcome = learner_obj.learn_with_usage([])
    assert outcome.result.rules == []
    assert any("threshold gate" in r.reason for r in outcome.result.rejected)
