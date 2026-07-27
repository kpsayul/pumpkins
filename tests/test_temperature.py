"""Tests for the per-stage sampling setting.

Review is pinned to 0 because its output goes straight to the user and the API
default made the same diff produce 0 findings on one run and 1 on the next.
Learn keeps the provider default on purpose — it is protected by the threshold
gate, reconcile and human approval before anything is enforced.

These assert the wiring, not the model: a constant nobody passes through is
just a comment.
"""

from pumpkins.config import LEARN_TEMPERATURE, REVIEW_TEMPERATURE
from pumpkins.conventions.learner import ConventionLearner, LearnResult
from pumpkins.llm.postprocess import LlmPostProcessor
from pumpkins.llm.provider import ParsedResult
from pumpkins.models import DiffScope


class _RecordingClient:
    """Stands in for a provider client and remembers how it was called."""

    def __init__(self, parsed):
        self.parsed = parsed
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return ParsedResult(parsed=self.parsed, input_tokens=0, output_tokens=0)


def _processor(monkeypatch) -> tuple[LlmPostProcessor, _RecordingClient]:
    from pumpkins.llm import postprocess

    client = _RecordingClient(postprocess._LlmReview(verdicts=[], extra_findings=[]))
    monkeypatch.setattr(postprocess, "get_client", lambda: client)
    return LlmPostProcessor(model="test-model"), client


def _learner(monkeypatch) -> tuple[ConventionLearner, _RecordingClient]:
    from pumpkins.conventions import learner

    client = _RecordingClient(LearnResult(rules=[]))
    monkeypatch.setattr(learner, "get_client", lambda: client)
    return ConventionLearner(model="test-model"), client


def test_config_pins_review_and_leaves_learn_alone():
    assert REVIEW_TEMPERATURE == 0.0
    assert LEARN_TEMPERATURE is None  # None → parameter omitted → provider default


def test_review_sends_temperature_zero(monkeypatch):
    processor, client = _processor(monkeypatch)
    processor.process(DiffScope(), [], shallow_mode=True)
    assert client.calls[0]["temperature"] == 0.0


def test_learn_leaves_temperature_to_the_provider(monkeypatch):
    learner_obj, client = _learner(monkeypatch)
    learner_obj.learn([])
    assert client.calls[0]["temperature"] is None


def test_none_temperature_is_omitted_from_the_request():
    """The client must not send `temperature=None` — some models reject the
    parameter outright, and a caller wanting the default should not have to
    know what that default is."""
    sent: dict = {}

    class _FakeMessages:
        def parse(self, **kwargs):
            sent.update(kwargs)
            raise _Stop

    class _Stop(Exception):
        pass

    from pumpkins.llm.provider import AnthropicClient

    client = AnthropicClient.__new__(AnthropicClient)  # skip SDK construction
    client._client = type("C", (), {"messages": _FakeMessages()})()

    for temperature, expected in ((None, False), (0.0, True)):
        sent.clear()
        try:
            client.parse(
                model="m", max_tokens=1, system="s", user="u", schema=LearnResult,
                temperature=temperature,
            )
        except _Stop:
            pass
        assert ("temperature" in sent) is expected
