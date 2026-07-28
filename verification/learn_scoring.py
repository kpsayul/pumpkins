"""learn 품질 채점을 위한 공유 헬퍼 (검증 하네스 전용, 제품 코드 아님).

`verification/` 아래에 두는 이유: 이 모듈은 pumpkins 파이프라인의 일부가 아니라
그 파이프라인을 *채점*하는 도구다. run_verification.py의 [5]/[6]과
score_real_repos.py가 함께 쓴다.

채점에 필요하지만 통계 데이터만으로는 안 되는 것을 여기서 다룬다:

  * learn 호출당 토큰 사용량 — 제품이 `ConventionLearner.learn_with_usage()`로 노출한다.
    여기 learn_with_usage()는 그 공개 API를 재시도로 감싸 채점용 LearnRun으로 포장할 뿐,
    프롬프트·게이트는 제품 코드가 소유한다. (예전엔 usage가 로그로만 남아 하네스가
    클라이언트를 직접 호출했으나, 그 API 공백은 메워졌다.)
  * 우리가 합성한 통계로 learn을 돌리는 길 — extract_stats의 출력은 그냥 데이터라
    게이트를 넘기도록 여기서 스케일한다 (픽스처가 게이트보다 작기 때문, 아래 참조).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


# --------------------------------------------------------------- 정답지(answer key)

@dataclass(frozen=True)
class ExpectedRule:
    """문서화된(또는 de-facto) 스타일 한 줄 — 채점의 정답.

    facet/value는 checker가 쓰는 기계 검증 형태와 같다 (prefix/suffix/casing).
    source는 이 규칙의 출처(리포의 CONTRIBUTING 등)를 적어 채점표에서 근거로 남긴다.
    """

    category: str
    facet: str  # prefix | suffix | casing
    value: str
    source: str = ""

    @property
    def triple(self) -> tuple[str, str, str]:
        return (self.category, self.facet, self.value)

    @property
    def axis(self) -> tuple[str, str]:
        return (self.category, self.facet)


_CHECKABLE_FACETS = ("prefix", "suffix", "casing")


def _rule_triple(rule) -> tuple[str, str, str]:
    """ConventionRule / ExpectedRule 공통으로 (category, facet, value)를 뽑는다."""
    return (rule.category, rule.facet, rule.value)


@dataclass
class Score:
    """정답지 대비 learn 결과의 채점.

    recall  = 정답 규칙 중 learn이 채택한 비율
    precision = 정답이 정의한 축(category,facet)에 채택된 규칙 중 값이 맞은 비율
              — 정답지가 다루지 않는 축(예: 우리가 판정 안 한 prefix 규칙)의
                추가 채택은 벌하지 않는다. 판정 근거가 없는 걸 틀렸다 할 수 없으니.
    """

    expected: list[tuple[str, str, str]]
    adopted: list[tuple[str, str, str]]
    matched: list[tuple[str, str, str]]
    missed: list[tuple[str, str, str]]
    wrong: list[tuple[str, str, str]]  # 정답 축에 채택됐지만 값이 틀린 것
    recall: float
    precision: float


def score_rules(adopted_rules, expected_rules: list[ExpectedRule]) -> Score:
    """learn이 채택한 규칙을 정답지와 대조해 recall/precision을 낸다.

    adopted_rules: ConventionRule 리스트 (learn 결과 .rules). facet=other는 자동
                   검증 불가라 제외 — checker와 같은 기준.
    """
    expected = {e.triple for e in expected_rules}
    axes = {e.axis for e in expected_rules}

    checkable = [r for r in adopted_rules if r.facet in _CHECKABLE_FACETS]
    adopted = {_rule_triple(r) for r in checkable}
    adopted_on_axes = {t for t in adopted if (t[0], t[1]) in axes}

    matched = expected & adopted
    missed = expected - adopted
    wrong = adopted_on_axes - expected  # 판정 축에 올렸지만 값이 다름

    recall = len(matched) / len(expected) if expected else 1.0
    precision = len(matched) / len(adopted_on_axes) if adopted_on_axes else 1.0

    return Score(
        expected=sorted(expected),
        adopted=sorted(adopted),
        matched=sorted(matched),
        missed=sorted(missed),
        wrong=sorted(wrong),
        recall=recall,
        precision=precision,
    )


# ----------------------------------------------------------- 관측 커버리지(근거)

def observed_coverage(stats, category: str, facet: str, value: str) -> tuple[int, int, float]:
    """정답 규칙이 통계상 얼마나 지지받는지 (count, denominator, fraction).

    채점표에 "이 규칙은 관측 71%라 게이트(85%)에 못 미쳐 채택 안 됨" 같은 근거를
    남기기 위한 것. denominator는 prefix/suffix면 category total, casing이면
    casing_informative (extractor가 casing 신호 없는 단어를 뺀 분모).
    """
    cat = next((s for s in stats if s.category == category), None)
    if cat is None:
        return (0, 0, 0.0)
    if facet == "prefix":
        counts, denom = cat.prefix_counts, cat.total
    elif facet == "suffix":
        counts, denom = cat.suffix_counts, cat.total
    elif facet == "casing":
        counts, denom = cat.casing_counts, cat.casing_informative
    else:
        return (0, 0, 0.0)
    count = counts.get(value, 0)
    frac = count / denom if denom else 0.0
    return (count, denom, frac)


# --------------------------------------------------------------- 통계 스케일

def scale_stats(stats, factor: int):
    """CategoryStats의 카운트를 정수배 — 분포는 보존하고 표본 수만 키운다.

    픽스처는 식별자가 6/6/2개뿐이라 MIN_RULE_OCCURRENCES(20) 게이트에 전부 걸린다
    (실측). 그대로 learn을 돌리면 정답과 무관하게 0규칙이 나와 채점이 무의미해진다.
    분포(100% m_ 등)를 유지한 채 표본만 현실적 규모로 키워, 측정 대상이 게이트가
    아니라 'LLM이 올바른 facet/value를 짚는가'가 되게 한다. 게이트 자체는 키 없이도
    돌아가는 결정적 단계에서 이미 검증된다.
    """
    scaled = []
    for s in stats:
        c = s.model_copy(deep=True)
        c.total *= factor
        c.casing_informative *= factor
        c.casing_ambiguous *= factor
        c.prefix_counts = {k: v * factor for k, v in c.prefix_counts.items()}
        c.suffix_counts = {k: v * factor for k, v in c.suffix_counts.items()}
        c.casing_counts = {k: v * factor for k, v in c.casing_counts.items()}
        scaled.append(c)
    return scaled


def scale_to_gate(stats, min_occurrences: int):
    """비어있지 않은 모든 category의 total이 게이트를 넘도록 최소 정수배로 스케일.

    반환: (scaled_stats, factor). factor==1이면 원본을 그대로 돌려준다.
    """
    totals = [s.total for s in stats if s.total > 0]
    if not totals:
        return stats, 1
    factor = max(1, math.ceil(min_occurrences / min(totals)))
    return (scale_stats(stats, factor) if factor > 1 else stats), factor


# ------------------------------------------------------------------- 모델·비용

# [6] 모델 비교의 tier 목록 — provider별로 저가→중간→강 3단. 활성 provider의 목록만
# 실제로 돈다. anthropic 세 tier(haiku/sonnet/opus)가 설계 문서 §4의 원래 판단
# 대상이고, openai 세 tier(gpt-4o-mini/gpt-4o/gpt-4.1)는 그에 대응하는 동종 비교다.
MODEL_TIERS = {
    "anthropic": [
        ("haiku", "claude-haiku-4-5-20251001"),
        ("sonnet", "claude-sonnet-5"),
        ("opus", "claude-opus-4-8"),
    ],
    "openai": [
        ("gpt-4o-mini", "gpt-4o-mini"),
        ("gpt-4o", "gpt-4o"),
        ("gpt-4.1", "gpt-4.1"),
    ],
}

# 근사 단가 (USD / 100만 토큰), (input, output). 공개 정가 기준의 *추정치*이며
# 시점에 따라 바뀐다 — 정확한 비용 단위는 위 토큰 수(정확값)이고, USD는 참고용.
# 값이 바뀌면 이 표만 고치면 된다. 모르는 모델은 None을 돌려준다.
PRICE_PER_MTOK = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o3-mini": (1.10, 4.40),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-4-8": (15.00, 75.00),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """근사 USD 비용. 단가 미상 모델이면 None (토큰 수로만 보고)."""
    price = PRICE_PER_MTOK.get(model)
    if price is None:
        return None
    price_in, price_out = price
    return input_tokens / 1e6 * price_in + output_tokens / 1e6 * price_out


@dataclass
class LearnRun:
    """learn 한 번의 결과 묶음 — 채점과 비용을 함께 나른다."""

    model: str
    rules: list  # list[ConventionRule]
    input_tokens: int
    output_tokens: int
    error: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float | None:
        return estimate_cost_usd(self.model, self.input_tokens, self.output_tokens)


def learn_with_usage(stats, model: str | None = None, attempts: int = 2) -> LearnRun:
    """learn을 돌리되 토큰 사용량까지 회수해 채점용 LearnRun으로 포장한다.

    `ConventionLearner.learn_with_usage()` 공개 API를 그대로 쓴다 — 규칙·게이트·프롬프트는
    제품 코드가 소유하고, 여기서는 채점에 필요한 usage만 받아 나른다.

    learn 온도는 제품 설정상 기본값(openai=1.0)이라 드물게 응답이 max_tokens에 걸려
    structured 파싱이 실패한다(고온도 과생성). 채점이 그 한 번의 흔들림에 좌우되지
    않도록 `attempts`회까지 재시도한다 — 하네스의 안정화이지 제품 동작 변경이 아니다.
    """
    from pumpkins.config import default_learn_model
    from pumpkins.conventions import ConventionLearner

    model = model or default_learn_model()
    learner = ConventionLearner(model=model)

    last_exc: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            outcome = learner.learn_with_usage(stats)
        except Exception as exc:  # 고온도 과생성으로 인한 length-limit 파싱 실패 등
            last_exc = exc
            continue
        return LearnRun(
            model=model,
            rules=outcome.result.rules,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
        )
    raise RuntimeError(f"{model}: learn 호출이 {attempts}회 모두 실패 — {last_exc}")


# ------------------------------------------------------------------- 비용 누적

@dataclass
class CostLog:
    """여러 learn 호출의 토큰/비용 누적 — 채점표 하단에 표로 남긴다."""

    runs: list[tuple[str, int, int, float | None]] = field(default_factory=list)  # (label, in, out, usd)

    def add(self, label: str, run: LearnRun) -> None:
        self.runs.append((label, run.input_tokens, run.output_tokens, run.cost_usd))

    @property
    def total_tokens(self) -> int:
        return sum(i + o for _, i, o, _ in self.runs)

    @property
    def total_usd(self) -> float | None:
        known = [u for _, _, _, u in self.runs if u is not None]
        return sum(known) if known else None

    def as_markdown(self) -> list[str]:
        lines = ["| 호출 | in tok | out tok | ≈USD |", "|---|---|---|---|"]
        for label, i, o, u in self.runs:
            usd = f"${u:.4f}" if u is not None else "—"
            lines.append(f"| {label} | {i} | {o} | {usd} |")
        total_usd = self.total_usd
        usd_cell = f"≈${total_usd:.4f}" if total_usd is not None else "—"
        lines.append(f"| **합계** | | {self.total_tokens} tok | {usd_cell} |")
        return lines
