"""AI rule-inference path (--infer). No API key: the provider client is
scripted, and the converter is a pure function."""

from pumpkins.conventions import (
    InferredRule,
    InferredRuleSet,
    RuleInferrer,
    RuleScope,
    to_convention_rules,
)
from pumpkins.conventions import proposer as proposer_mod
from pumpkins.llm.provider import ParsedResult


class _ScriptedClient:
    def __init__(self, parsed, in_tok=300, out_tok=80):
        self._result = ParsedResult(parsed=parsed, input_tokens=in_tok, output_tokens=out_tok)
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


def _install(monkeypatch, client):
    monkeypatch.setattr(proposer_mod, "get_client", lambda: client)


def test_infer_reads_code_and_returns_rules(tmp_path, monkeypatch):
    (tmp_path / "a.h").write_text("#pragma once\nclass Foo { int m_x; };\n", encoding="utf-8")
    parsed = InferredRuleSet(
        rules=[
            InferredRule(
                rule="헤더는 `#pragma once`를 쓴다", kind="layout",
                evidence="a.h starts with #pragma once",
                mechanical_check="check header first non-comment line",
                checkable_with_ast=False,
            )
        ]
    )
    client = _ScriptedClient(parsed, in_tok=321, out_tok=45)
    _install(monkeypatch, client)

    outcome = RuleInferrer(model="strong").infer(tmp_path)

    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "strong"
    assert client.calls[0]["schema"] is InferredRuleSet
    # the actual source is what got sent (facet-free — code, not statistics)
    assert "#pragma once" in client.calls[0]["user"]
    assert outcome.files_read == 1
    assert outcome.input_tokens == 321 and outcome.output_tokens == 45
    assert outcome.rules[0].kind == "layout"


def test_infer_skips_the_call_when_no_cpp_files(tmp_path, monkeypatch):
    (tmp_path / "readme.md").write_text("not code", encoding="utf-8")
    client = _ScriptedClient(InferredRuleSet(rules=[]))
    _install(monkeypatch, client)

    outcome = RuleInferrer(model="strong").infer(tmp_path)

    assert client.calls == []            # no code → no tokens spent
    assert outcome.rules == []
    assert outcome.total_tokens == 0


def test_to_convention_rules_makes_unverified_facet_other_candidates():
    inferred = [
        InferredRule(rule="헤더는 `#pragma once`를 쓴다", kind="layout", evidence="a.h"),
        InferredRule(rule="팩토리는 unique_ptr를 반환한다", kind="ownership", evidence="make*"),
    ]
    scope = RuleScope(paths=["src/**"])
    rules = to_convention_rules(inferred, scope)

    assert [r.facet for r in rules] == ["other", "other"]
    # unverified guesses: no measured numbers, low confidence — so the threshold
    # gate (for measured rules) is bypassed and a human is the gate.
    assert all(r.coverage == 0.0 and r.occurrences == 0 for r in rules)
    assert all(r.confidence == "low" for r in rules)
    assert rules[0].category == "layout" and rules[1].category == "ownership"
    assert rules[0].examples == ["a.h"]           # evidence kept
    assert rules[0].scope.paths == ["src/**"]     # scan scope propagated
    assert rules[0].id.startswith("ai-")          # marked as an AI-inferred guess


def test_to_convention_rules_gives_unique_ids_for_duplicate_text():
    inferred = [InferredRule(rule="같은 규칙"), InferredRule(rule="같은 규칙")]
    rules = to_convention_rules(inferred)
    assert rules[0].id != rules[1].id             # no candidate file clobbering


def test_to_convention_rules_drops_empty_rule_text():
    assert to_convention_rules([InferredRule(rule="   ")]) == []
