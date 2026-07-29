"""AI rule-inference path (--infer). No API key: the provider client is
scripted, and the converter is a pure function."""

from pumpkins.conventions import (
    InferenceLead,
    InferredRule,
    InferredRuleSet,
    RuleInferrer,
    RuleScope,
    TriageResult,
    to_convention_rules,
)
from pumpkins.conventions import proposer as proposer_mod
from pumpkins.llm.provider import ParsedResult


class _ScriptedClient:
    """Answers by requested schema, so a two-pass run can be scripted per pass."""

    def __init__(self, by_schema: dict, in_tok=300, out_tok=80):
        self._by_schema = by_schema
        self._in, self._out = in_tok, out_tok
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return ParsedResult(
            parsed=self._by_schema.get(kwargs["schema"]),
            input_tokens=self._in,
            output_tokens=self._out,
        )

    def call_for(self, schema) -> dict | None:
        return next((c for c in self.calls if c["schema"] is schema), None)


def _install(monkeypatch, client):
    monkeypatch.setattr(proposer_mod, "get_client", lambda: client)


_PRAGMA_RULE = InferredRuleSet(
    rules=[
        InferredRule(
            rule="헤더는 `#pragma once`를 쓴다", kind="layout",
            evidence="a.h starts with #pragma once",
            mechanical_check="check header first non-comment line",
            checkable_with_ast=False,
        )
    ]
)


def test_infer_reads_code_and_returns_rules(tmp_path, monkeypatch):
    (tmp_path / "a.h").write_text("#pragma once\nclass Foo { int m_x; };\n", encoding="utf-8")
    client = _ScriptedClient({InferredRuleSet: _PRAGMA_RULE}, in_tok=321, out_tok=45)
    _install(monkeypatch, client)

    outcome = RuleInferrer(model="strong", triage=False).infer(tmp_path)

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
    client = _ScriptedClient({InferredRuleSet: InferredRuleSet(rules=[])})
    _install(monkeypatch, client)

    outcome = RuleInferrer(model="strong", triage=False).infer(tmp_path)

    assert client.calls == []            # no code → no tokens spent
    assert outcome.rules == []
    assert outcome.total_tokens == 0


# ------------------------------------------------- 2단계: 싼 모델이 먼저 훑는다

def _repo_with_two_areas(tmp_path):
    (tmp_path / "src" / "core").mkdir(parents=True)
    (tmp_path / "src" / "ui").mkdir(parents=True)
    (tmp_path / "src" / "core" / "engine.h").write_text(
        "#pragma once\nclass Engine { std::unique_ptr<Impl> m_impl; };\n", encoding="utf-8"
    )
    (tmp_path / "src" / "ui" / "window.h").write_text(
        '#pragma once\n#include "engine.h"\nclass Window { Engine* m_engine; };\n',
        encoding="utf-8",
    )
    return tmp_path


def test_triage_picks_the_files_the_strong_model_reads(tmp_path, monkeypatch):
    """The cost claim of the two-stage design: the strong model sees the
    nominated file and NOT the rest of the repo."""
    repo = _repo_with_two_areas(tmp_path)
    client = _ScriptedClient(
        {
            TriageResult: TriageResult(
                leads=[
                    InferenceLead(
                        area="src/core",
                        suspicion="core 는 ui 를 참조하지 않는 것 같다",
                        files=["src/core/engine.h"],
                    )
                ]
            ),
            InferredRuleSet: _PRAGMA_RULE,
        }
    )
    _install(monkeypatch, client)

    outcome = RuleInferrer(model="strong", triage_model="cheap").infer(repo)

    triage_call = client.call_for(TriageResult)
    infer_call = client.call_for(InferredRuleSet)
    assert triage_call["model"] == "cheap" and infer_call["model"] == "strong"
    # the cheap pass reads a structure map, not source
    assert "Include edges" in triage_call["user"]
    assert "unique_ptr" not in triage_call["user"] or "class Engine {" not in triage_call["user"]
    # the strong pass reads the SOURCE of only what triage nominated...
    assert "// ===== src/core/engine.h =====" in infer_call["user"]
    assert "// ===== src/ui/window.h =====" not in infer_call["user"]
    assert "class Window" not in infer_call["user"]
    # ...but still gets the whole-repo structure map, because some conventions
    # exist only in the aggregate: no sample of files contains "src includes
    # include/ 81 times and never the reverse".
    assert "Include edges" in infer_call["user"]
    assert "src/ui -> src/core" in infer_call["user"]
    # the suspicion travels as a question, not as a finding
    assert "core 는 ui 를 참조하지 않는 것 같다" in infer_call["user"]
    assert "UNVERIFIED" in infer_call["user"]
    assert outcome.triaged and outcome.files_read == 1
    assert outcome.triage_input_tokens > 0


def test_no_leads_means_the_strong_model_never_runs(tmp_path, monkeypatch):
    """Nothing to look at is a real answer — and the cheap one."""
    repo = _repo_with_two_areas(tmp_path)
    client = _ScriptedClient(
        {TriageResult: TriageResult(leads=[]), InferredRuleSet: _PRAGMA_RULE}
    )
    _install(monkeypatch, client)

    outcome = RuleInferrer(model="strong", triage_model="cheap").infer(repo)

    assert client.call_for(InferredRuleSet) is None
    assert outcome.rules == [] and outcome.files_read == 0
    assert outcome.total_tokens == outcome.triage_input_tokens + outcome.triage_output_tokens


def test_a_lead_pointing_outside_the_scan_is_dropped(tmp_path, monkeypatch):
    """Scoping still holds: triage cannot widen the scan by naming a path the
    learn scan excluded."""
    repo = _repo_with_two_areas(tmp_path)
    client = _ScriptedClient(
        {
            TriageResult: TriageResult(
                leads=[InferenceLead(area="etc", files=["/etc/passwd", "vendor/zlib.h"])]
            ),
            InferredRuleSet: _PRAGMA_RULE,
        }
    )
    _install(monkeypatch, client)

    outcome = RuleInferrer(triage_model="cheap").infer(repo)

    assert client.call_for(InferredRuleSet) is None
    assert outcome.files_read == 0


def test_a_lead_with_a_near_miss_path_still_resolves(tmp_path, monkeypatch):
    """A model copying a path out of the map may drop the leading directory.
    That is a formatting slip, not a reason to waste the lead."""
    repo = _repo_with_two_areas(tmp_path)
    client = _ScriptedClient(
        {
            TriageResult: TriageResult(leads=[InferenceLead(area="ui", files=["window.h"])]),
            InferredRuleSet: _PRAGMA_RULE,
        }
    )
    _install(monkeypatch, client)

    outcome = RuleInferrer(triage_model="cheap").infer(repo)

    assert outcome.files_read == 1
    assert "window.h" in client.call_for(InferredRuleSet)["user"]


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
