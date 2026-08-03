"""안 돌아가는 검사를 한 번 고쳐 쓰게 하기.

실측 근거: 리포 전체를 읽으며 쓴 검사 20개 중 질의 17개가 컴파일조차 안 됐고,
잴 수 있었던 규칙은 3개뿐이었다. 그 관찰들 자체는 대체로 맞았다 — 모델이 코드는
제대로 읽고 나서 '어떻게 세는지'를 못 쓴 것이다. 초안 문법 오류로 그걸 잃는 건
가장 값싼 낭비다."""

import os

import pytest

from pumpkins.conventions import InferredRule, RuleCheck, RuleInferrer
from pumpkins.conventions import proposer as proposer_mod
from pumpkins.conventions.proposer import RepairSet, RepairedCheck, diagnose_check
from pumpkins.languages.cpp import query as cpp_query
from pumpkins.llm.provider import ParsedResult

requires_ts = pytest.mark.skipif(
    not cpp_query.available(), reason="PUMPKINS_ALLOW_NO_TREE_SITTER=1 (명시적 건너뛰기)"
)

_GOOD_POP = ("(field_declaration (function_declarator "
             "declarator: (field_identifier) @subject (parameter_list)))")
_GOOD_CONF = ("(field_declaration (function_declarator "
              "declarator: (field_identifier) @subject (type_qualifier)))")


class _ScriptedClient:
    def __init__(self, fixes):
        self._fixes = fixes
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return ParsedResult(parsed=RepairSet(fixed=self._fixes), input_tokens=200, output_tokens=50)


def _repo(tmp_path, n=8):
    (tmp_path / "src").mkdir()
    for i in range(n):
        (tmp_path / "src" / f"w{i}.h").write_text(
            f"class W{i} {{\n public:\n  int a() const;\n  int b() const;\n"
            f"  int c();\n  int d();\n}};\n", encoding="utf-8"
        )
    return tmp_path


# ------------------------------------------------------------------ 진단

@requires_ts
def test_a_query_that_does_not_compile_says_why(tmp_path):
    """오류 메시지를 그대로 돌려준다 — 'virtual 은 익명 노드' 같은 건
    '틀렸습니다' 보다 훨씬 고치기 쉬운 피드백이다."""
    bad = "(field_declaration (function_declarator (virtual) @subject))"
    reason = diagnose_check(_repo(tmp_path), RuleCheck(
        kind="query", population_query=bad, conforming_query=_GOOD_CONF))
    assert "does not compile" in reason
    assert "virtual" in reason          # 어느 토큰이 문제인지가 들어 있다


@requires_ts
def test_a_conforming_query_matching_nothing_is_diagnosed(tmp_path):
    """대상은 다 찾았는데 만족이 0이면 리포 탓이 아니라 질의 탓이다."""
    reason = diagnose_check(_repo(tmp_path), RuleCheck(
        kind="query",
        population_query=_GOOD_POP,
        conforming_query='(field_declaration (function_declarator '
                         'declarator:(field_identifier) @subject (virtual_specifier) '
                         '(type_qualifier) (parameter_list) (parameter_list)))'))
    assert "0 of them" in reason and "conforming query is almost certainly wrong" in reason


@requires_ts
def test_a_working_check_is_not_flagged(tmp_path):
    assert diagnose_check(_repo(tmp_path), RuleCheck(
        kind="query", population_query=_GOOD_POP, conforming_query=_GOOD_CONF)) is None


def test_non_query_kinds_are_left_alone(tmp_path):
    """손으로 쓴 검사는 우리 코드라 고쳐 쓸 대상이 아니다."""
    assert diagnose_check(tmp_path, RuleCheck(
        kind="naming", category="member", facet="prefix", value="m_")) is None


# ------------------------------------------------------------------ 고쳐쓰기

@requires_ts
def test_a_broken_check_is_repaired_and_the_rule_survives(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    rules = [InferredRule(rule="인자 없는 메서드는 const 를 붙인다", check=RuleCheck(
        kind="query",
        population_query="(field_declaration (function_declarator (virtual) @subject))",
        conforming_query=_GOOD_CONF))]
    client = _ScriptedClient([RepairedCheck(index=0, population_query=_GOOD_POP,
                                            conforming_query=_GOOD_CONF)])
    monkeypatch.setattr(proposer_mod, "get_client", lambda: client)

    outcome = RuleInferrer(model="strong").repair_checks(repo, rules)

    assert outcome.attempted == 1 and outcome.repaired == 1
    assert rules[0].check.population_query == _GOOD_POP      # 규칙 문장은 그대로
    assert rules[0].rule == "인자 없는 메서드는 const 를 붙인다"


@requires_ts
def test_a_repair_that_is_also_broken_is_rejected(tmp_path, monkeypatch):
    """두 번째 시도가 첫 번째보다 믿을 만할 이유가 없다 — 돌아가야 채택된다."""
    repo = _repo(tmp_path)
    original = "(field_declaration (function_declarator (virtual) @subject))"
    rules = [InferredRule(rule="…", check=RuleCheck(
        kind="query", population_query=original, conforming_query=_GOOD_CONF))]
    client = _ScriptedClient([RepairedCheck(index=0,
                                            population_query="(also_bogus) @subject",
                                            conforming_query=_GOOD_CONF)])
    monkeypatch.setattr(proposer_mod, "get_client", lambda: client)

    outcome = RuleInferrer(model="strong").repair_checks(repo, rules)

    assert outcome.repaired == 0 and outcome.still_broken == 1
    assert rules[0].check.population_query == original       # 원본을 지킨다


@requires_ts
def test_all_failures_go_in_one_call(tmp_path, monkeypatch):
    """17건 실패가 17번 호출이 되면 재시도가 원래 통과보다 비싸진다."""
    repo = _repo(tmp_path)
    bad = "(field_declaration (function_declarator (virtual) @subject))"
    rules = [
        InferredRule(rule=f"규칙{i}", check=RuleCheck(
            kind="query", population_query=bad, conforming_query=_GOOD_CONF))
        for i in range(6)
    ]
    client = _ScriptedClient([])
    monkeypatch.setattr(proposer_mod, "get_client", lambda: client)

    outcome = RuleInferrer(model="strong").repair_checks(repo, rules)

    assert len(client.calls) == 1                 # 6건이 한 번에
    assert outcome.attempted == 6
    # 각 실패가 번호와 이유를 달고 들어간다
    for i in range(6):
        assert f"[{i}]" in client.calls[0]["user"]
    assert "PROBLEM:" in client.calls[0]["user"]


@requires_ts
def test_nothing_broken_means_no_call(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    rules = [InferredRule(rule="…", check=RuleCheck(
        kind="query", population_query=_GOOD_POP, conforming_query=_GOOD_CONF))]
    client = _ScriptedClient([])
    monkeypatch.setattr(proposer_mod, "get_client", lambda: client)

    outcome = RuleInferrer(model="strong").repair_checks(repo, rules)

    assert client.calls == [] and outcome.attempted == 0


@requires_ts
def test_the_repair_prompt_carries_the_node_vocabulary(tmp_path, monkeypatch):
    """고칠 때야말로 진짜 노드 이름이 필요하다 — 실패 원인 1등이 이름 짐작이다."""
    repo = _repo(tmp_path)
    rules = [InferredRule(rule="…", check=RuleCheck(
        kind="query",
        population_query="(field_declaration (function_declarator (virtual) @subject))",
        conforming_query=_GOOD_CONF))]
    client = _ScriptedClient([])
    monkeypatch.setattr(proposer_mod, "get_client", lambda: client)

    RuleInferrer(model="strong").repair_checks(repo, rules)

    assert "(field_declaration)" in client.calls[0]["user"]
    assert '"virtual"' in client.calls[0]["user"]


# --------------------------------------- 우리 어휘 밖의 값은 0% 가 아니라 측정 불가

@requires_ts
def test_an_unknown_casing_value_is_unmeasurable_not_zero_percent(tmp_path):
    """실측: 모델이 `UpperCamel` 대신 `UpperCamelCase` 라고 썼고, yaml-cpp 의
    클래스 347개가 전부 지키는 규칙이 0/347 로 기각됐다. 우리가 모르는 값이면
    '레포가 안 지킨다'가 아니라 '못 쟀다'가 맞는 답이다."""
    from pumpkins.conventions import verify

    (tmp_path / "a.h").write_text(
        "\n".join(f"class GoodName{i} {{}};" for i in range(30)), encoding="utf-8"
    )
    good = verify(tmp_path, RuleCheck(
        kind="naming", category="class_type", facet="casing", value="UpperCamel"))
    assert good.coverage == 1.0

    typo = verify(tmp_path, RuleCheck(
        kind="naming", category="class_type", facet="casing", value="UpperCamelCase"))
    assert typo is None            # 0% 로 오답 처리하지 않는다


def test_the_casing_vocabulary_matches_what_the_scanner_produces():
    """검사가 받는 값의 집합과 스캐너가 뱉는 값의 집합이 어긋나면, 그 틈으로
    조용한 0% 가 새어 나온다."""
    from pumpkins.languages.cpp.naming import KNOWN_CASINGS, split_pattern

    produced = {
        split_pattern(name)[2]
        for name in ("maxCount", "max_count", "MaxCount", "MAX_COUNT", "flush", "X9_a")
    }
    assert produced <= KNOWN_CASINGS
