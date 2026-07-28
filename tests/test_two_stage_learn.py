"""Two-stage learn (design doc §4.3): stage-1 classification on the cheap tier,
stage-2 split-boundary naming escalated to the strong tier — but only when a
split is detected and only over the split groups. No API key needed; the
provider client is scripted.
"""

from pumpkins.conventions import (
    CategoryStats,
    ConventionLearner,
    LearnResult,
    SplitAdjudication,
    SplitHypothesis,
)
from pumpkins.conventions import learner as learner_mod
from pumpkins.llm.provider import ParsedResult


class _ScriptedClient:
    """Returns a pre-scripted ParsedResult per call, recording each call's kwargs."""

    def __init__(self, *results: ParsedResult):
        self._results = list(results)
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        result = self._results[len(self.calls)]
        self.calls.append(kwargs)
        return result


def _install(monkeypatch, client: _ScriptedClient) -> None:
    monkeypatch.setattr(learner_mod, "get_client", lambda: client)


def _split_stats() -> CategoryStats:
    """A category that reads as two groups, not one convention (72/28) — the
    shape detect_split_signal fires on, so stage 2 should run."""
    return CategoryStats(
        category="private_member",
        total=100,
        suffix_counts={"_": 72, "(none)": 28},
        facet_samples={"suffix=_": ["queue_", "size_"], "suffix=(none)": ["mCount"]},
        facet_dirs={"suffix=_": ["src (72)"], "suffix=(none)": ["vendor (28)"]},
    )


def _clean_stats() -> CategoryStats:
    """A category with a clear dominant pattern — no split, no escalation."""
    return CategoryStats(
        category="private_member",
        total=100,
        suffix_counts={"_": 95, "(none)": 5},
    )


def _stage1(rules=None, in_tok=100, out_tok=10) -> ParsedResult:
    return ParsedResult(
        parsed=LearnResult(rules=rules or []), input_tokens=in_tok, output_tokens=out_tok
    )


def _stage2(hypotheses=None, in_tok=50, out_tok=5) -> ParsedResult:
    return ParsedResult(
        parsed=SplitAdjudication(split_hypotheses=hypotheses or []),
        input_tokens=in_tok,
        output_tokens=out_tok,
    )


def test_split_escalates_to_reasoning_model(monkeypatch):
    hyp = SplitHypothesis(
        category="private_member", facet="suffix",
        groups="_ (72%) vs (none) (28%)",
        discriminator="vendor/ is a bundled library", checkable=True,
    )
    client = _ScriptedClient(_stage1(), _stage2([hyp]))
    _install(monkeypatch, client)

    learner = ConventionLearner(model="cheap", reasoning_model="strong")
    outcome = learner.learn_with_usage([_split_stats()])

    # Two calls: stage 1 on the cheap tier, stage 2 on the strong tier.
    assert [c["model"] for c in client.calls] == ["cheap", "strong"]
    assert client.calls[1]["schema"] is SplitAdjudication
    # Stage 2's hypotheses win — they came from the model that can name a split.
    assert outcome.result.split_hypotheses == [hyp]
    # Usage is the sum of both stages, so cost lands as one number.
    assert outcome.input_tokens == 150
    assert outcome.output_tokens == 15


def test_no_split_makes_a_single_cheap_call(monkeypatch):
    client = _ScriptedClient(_stage1())
    _install(monkeypatch, client)

    learner = ConventionLearner(model="cheap", reasoning_model="strong")
    outcome = learner.learn_with_usage([_clean_stats()])

    assert [c["model"] for c in client.calls] == ["cheap"]
    assert outcome.result.split_hypotheses == []
    assert outcome.input_tokens == 100


def test_no_escalate_flag_keeps_everything_on_stage1(monkeypatch):
    client = _ScriptedClient(_stage1())
    _install(monkeypatch, client)

    learner = ConventionLearner(model="cheap", reasoning_model="strong", escalate=False)
    outcome = learner.learn_with_usage([_split_stats()])  # split present but ignored

    assert [c["model"] for c in client.calls] == ["cheap"]
    assert outcome.result.split_hypotheses == []


def test_escalation_skipped_when_stage1_already_uses_the_strong_model(monkeypatch):
    """Running learn with the strong model as stage 1 gains nothing from a second
    call to the same model — the guard collapses the two stages into one."""
    client = _ScriptedClient(_stage1())
    _install(monkeypatch, client)

    learner = ConventionLearner(model="strong", reasoning_model="strong")
    learner.learn_with_usage([_split_stats()])

    assert [c["model"] for c in client.calls] == ["strong"]
