#!/usr/bin/env python3
"""pumpkins 자기 검증 드라이버 — MVP 3단계 (verification-plan Track A 자동화).

전체 flow를 처음부터 끝까지 실행하되, API 키가 필요한 단계는 스켈레톤이라
자동으로 skip 처리된다 (키가 없어도 flow는 끝까지 돈다):

  [1] checker 주입 검증   (키 불필요 — 실제 실행)
      fixture 레포 복사 → git 초기화·커밋 → 위반 5건 + 무해 변경 3건 주입
      → conventions.yml 대조 → 주입 정답지와 비교해 recall/precision 산출
  [2] CLI 스모크          (키 불필요 — 실제 실행; clang-tidy 없으면 skip)
      `pumpkins --no-llm` 전체 파이프라인이 리포트까지 뽑는지 확인
  [3] scope 격리 검증     (키 불필요 — 실제 실행)
      legacy/ 하위에 같은 위반을 심고 exclude_paths로 제외 → legacy는 침묵하고
      src의 지적은 그대로 남는지 (규칙 scope의 계약)
  [4] 저장소 포맷 등가성   (키 불필요 — 실제 실행)
      레거시 conventions.yml과 conventions/ 저장소가 같은 지적을 내는지 +
      미승인 후보(candidates/)가 리뷰에 적용되지 않는지
  [5] learn 품질 검증     (키 필요 — 구현됨: fixture 통계로 learn을 돌려 채택된
      규칙을 정답지(fixture/conventions.yml)와 (category,facet,value)로 비교.
      픽스처는 식별자가 게이트보다 적어 통계를 게이트 위로 스케일해 LLM의 패턴
      식별력을 잰다 — learn_scoring.scale_to_gate 참조)
  [6] 모델 비교           (키 필요 — 구현됨: 같은 통계를 활성 provider의 tier들로
      판정시켜 규칙 정확도·토큰·비용 비교. anthropic이면 haiku/sonnet/opus로
      설계 문서 §4를 판정 — 단 실제 ANTHROPIC_API_KEY가 있어야 그 tier가 돈다)
  [7] 리뷰 LLM triage e2e (키 필요 — 스켈레톤; 이번 작업 범위 아님)

정답이 공개된 실제 오픈소스 리포(fmt/googletest/Catch2)로 learn을 채점하는
확장은 별도 스크립트다: `python verification/score_real_repos.py`
(docs/verification-plan.md "픽스처 검증의 한계" 참조).

사용:
    python verification/run_verification.py [--keep-workdir]

채점표는 verification/results/ 에 남는다. exit code: [1]의 recall/precision이
목표선(각 0.8) 미달이면 1.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from pumpkins.config import (
    MIN_RULE_OCCURRENCES,
    current_provider,
    default_learn_model,
    has_api_key,
    required_key_env,
    setup_logging,
)
from pumpkins.conventions import (
    RuleScope,
    StoredRule,
    check_scope,
    extract_stats,
    load_conventions,
    write_rule,
)
from pumpkins.diff import collect_diff

# 같은 디렉터리의 채점 헬퍼 — 이 파일은 스크립트로 실행되므로 verification/이
# sys.path[0]에 올라 flat import가 된다 (score_real_repos.py도 동일).
import learn_scoring  # noqa: E402

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "fixture"
RESULTS = ROOT / "results"
PASS_LINE = 0.8  # 주입 검증 통과선 (fixture는 통제 환경 — 사실상 1.0이 기대값)

load_dotenv(find_dotenv(usecwd=True))  # .env의 LLM_PROVIDER/키 반영 — 실제 환경변수가 우선 (CLI와 동일)
PROVIDER = current_provider()
KEY_ENV = required_key_env()
HAS_KEY = has_api_key()


# ------------------------------------------------------------- injection spec

@dataclass
class Injection:
    """fixture에 가할 한 건의 변경. expected가 비어 있으면 무해 변경(오탐 측정용)."""

    label: str
    file: str
    old: str
    new: str
    expected: list[tuple[str, str]] = field(default_factory=list)  # (rule_id, name)


INJECTIONS = [
    # --- 위반 5건 -------------------------------------------------------------
    Injection(
        "멤버 m_ 누락 (private 깊숙이 — hunk 헤더 문맥 의존)",
        "src/thread_pool.h",
        "    bool m_running;",
        "    bool m_running;\n    int counter;",
        expected=[("member-prefix-m_", "counter")],
    ),
    Injection(
        "멤버 잘못된 접두사 (_)",
        "src/thread_pool.h",
        "    int m_interval;",
        "    int m_interval;\n    float _ratio;",
        expected=[("member-prefix-m_", "_ratio")],
    ),
    Injection(
        "함수 casing 위반 (선언)",
        "src/thread_pool.h",
        "    void drainQueue();",
        "    void drainQueue();\n    void Flush_All();",
        expected=[("function-casing-lowerCamel", "Flush_All")],
    ),
    Injection(
        "함수 casing 위반 (정의, snake_case)",
        "src/worker.cpp",
        "int ThreadPool::pendingCount() const {\n    return m_capacity;\n}",
        "int ThreadPool::pendingCount() const {\n    return m_capacity;\n}\n\n"
        "void run_task(ThreadPool& pool) {\n    (void)pool;\n}",
        expected=[("function-casing-lowerCamel", "run_task")],
    ),
    Injection(
        "클래스 casing 위반",
        "src/thread_pool.h",
        "    ThreadPool* m_pool;\n};",
        "    ThreadPool* m_pool;\n};\n\nclass badParser {\n};",
        expected=[("class-casing-UpperCamel", "badParser")],
    ),
    # --- 무해 변경 3건 (지적되면 안 됨 — precision 측정) ------------------------
    Injection(
        "무해: 관행 준수 멤버 추가",
        "src/thread_pool.h",
        "    int m_capacity;",
        "    int m_capacity;\n    int m_extraSlot;",
    ),
    Injection(
        "무해: 관행 준수 함수 추가",
        "src/worker.cpp",
        '#include "thread_pool.h"',
        '#include "thread_pool.h"\n\nvoid extraHelper() {\n}',
    ),
    Injection(
        "무해: 함수 안 지역 변수 (멤버 규칙 비대상)",
        "src/worker.cpp",
        "    int localTotal = id;",
        "    int localTotal = id;\n    int count = id;",
    ),
]


# ----------------------------------------------------------------- step 결과

@dataclass
class StepResult:
    name: str
    status: str  # "pass" | "fail" | "skipped"
    detail: str = ""


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    # encoding 고정: 자식(pumpkins.cli 등)은 stdout을 UTF-8로 내보내는데, 부모가
    # locale(한국어 Windows는 cp949)로 디코딩하면 em대시 등에서 UnicodeDecodeError로
    # 죽어 proc.stdout이 None이 된다. UTF-8로 읽고, 혹시 모를 바이트는 replace.
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def setup_workdir(keep: bool) -> Path:
    workdir = Path(tempfile.mkdtemp(prefix="pumpkins-verify-"))
    shutil.copytree(FIXTURE, workdir, dirs_exist_ok=True)
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "verify@pumpkins"],
        ["git", "config", "user.name", "pumpkins-verify"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "fixture baseline"],
    ):
        proc = _run(cmd, workdir)
        if proc.returncode != 0:
            raise RuntimeError(f"git setup failed: {proc.stderr}")

    for inj in INJECTIONS:
        path = workdir / inj.file
        text = path.read_text(encoding="utf-8")
        if inj.old not in text:
            raise RuntimeError(f"injection anchor not found ({inj.label}): {inj.old!r}")
        path.write_text(text.replace(inj.old, inj.new, 1), encoding="utf-8")

    print(f"workdir: {workdir}" + ("" if keep else " (자동 삭제)"))
    return workdir


# ------------------------------------------------------------------- 단계들

def step_checker_injection(workdir: Path) -> tuple[StepResult, dict]:
    """[1] 주입 위반을 checker가 재현하는가 — recall/precision (키 불필요)."""
    scope = collect_diff(workdir)
    rules = load_conventions(workdir / "conventions.yml")
    findings = check_scope(scope, rules)

    # rule id는 evidence에서 바로 읽는다 — 예전에는 f.check 문자열에서
    # "convention:" 접두사를 벗겨냈는데, 그건 계약이 아닌 포맷을 파싱하는 것이었다.
    observed = {
        (f.evidence.rule_id, m.group(1))
        for f in findings
        if f.evidence.rule_id and (m := re.match(r"`([^`]+)`", f.title))
    }
    expected = {pair for inj in INJECTIONS for pair in inj.expected}

    tp = observed & expected
    missed = expected - observed
    false_pos = observed - expected
    recall = len(tp) / len(expected) if expected else 1.0
    precision = len(tp) / len(observed) if observed else 1.0

    metrics = {
        "expected": sorted(expected), "missed": sorted(missed),
        "false_positives": sorted(false_pos),
        "recall": recall, "precision": precision,
    }
    ok = recall >= PASS_LINE and precision >= PASS_LINE
    detail = (
        f"recall {recall:.0%} ({len(tp)}/{len(expected)}), "
        f"precision {precision:.0%} ({len(tp)}/{len(observed) or 1})"
        + (f"; missed: {sorted(missed)}" if missed else "")
        + (f"; false positives: {sorted(false_pos)}" if false_pos else "")
    )
    return StepResult("[1] checker 주입 검증", "pass" if ok else "fail", detail), metrics


def step_cli_smoke(workdir: Path) -> StepResult:
    """[2] `pumpkins --no-llm` 전체 파이프라인 스모크 (키 불필요)."""
    if shutil.which("clang-tidy") is None:
        return StepResult("[2] CLI 스모크", "skipped", "clang-tidy not installed")
    proc = subprocess.run(
        [sys.executable, "-m", "pumpkins.cli", "--repo", str(workdir), "--no-llm"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        return StepResult("[2] CLI 스모크", "fail", proc.stderr.strip()[-300:])
    if "- conventions: 3 active rule(s)" not in proc.stdout:
        return StepResult("[2] CLI 스모크", "fail", "report missing conventions header")
    n = proc.stdout.count("· convention ·")
    if "**재현성:**" not in proc.stdout:
        return StepResult("[2] CLI 스모크", "fail", "report missing reproducibility summary")
    return StepResult("[2] CLI 스모크", "pass", f"report OK, convention finding {n}건 포함")


def step_scope_isolation(workdir: Path) -> StepResult:
    """[3] 규칙 scope가 하위 트리를 실제로 비껴가는가 (키 불필요).

    같은 위반(`m_` 누락)을 legacy/ 에 심고, 규칙에 exclude_paths=["legacy"]를
    준 전후를 비교한다. 통과 조건은 둘 다여야 한다 — legacy는 침묵하고,
    src의 기존 지적 5건은 그대로 남는 것. 뒤쪽이 없으면 scope는 그냥
    체커를 끄는 스위치일 뿐이다.
    """
    legacy = workdir / "legacy"
    legacy.mkdir(exist_ok=True)
    (legacy / "legacy_pool.h").write_text(
        "class LegacyPool {\npublic:\n    void oldStyle();\n\nprivate:\n"
        "    int count;\n};\n",
        encoding="utf-8",
    )
    if _run(["git", "add", "legacy"], workdir).returncode != 0:
        return StepResult("[3] scope 격리 검증", "fail", "git add legacy 실패")

    scope = collect_diff(workdir)
    rules = load_conventions(workdir / "conventions.yml")

    before = check_scope(scope, rules)
    hit_legacy = [f for f in before if f.file.startswith("legacy/")]

    for rule in rules:
        rule.scope = RuleScope(exclude_paths=["legacy"])
    after = check_scope(scope, rules)
    still_legacy = [f for f in after if f.file.startswith("legacy/")]
    still_src = [f for f in after if not f.file.startswith("legacy/")]

    if not hit_legacy:
        return StepResult(
            "[3] scope 격리 검증", "fail",
            "scope 없이도 legacy 위반을 못 잡음 — 검증이 성립하지 않음",
        )
    if still_legacy:
        return StepResult(
            "[3] scope 격리 검증", "fail",
            f"exclude_paths 적용 후에도 legacy에서 {len(still_legacy)}건 지적",
        )
    if len(still_src) != len(before) - len(hit_legacy):
        return StepResult(
            "[3] scope 격리 검증", "fail",
            f"scope가 범위 밖까지 껐음 — src 지적 {len(still_src)}건 "
            f"(기대 {len(before) - len(hit_legacy)}건)",
        )
    return StepResult(
        "[3] scope 격리 검증", "pass",
        f"legacy {len(hit_legacy)}건 → 0건, src {len(still_src)}건 유지",
    )


def step_store_format_equivalence(workdir: Path) -> StepResult:
    """[4] 레거시 conventions.yml과 conventions/ 저장소가 같은 결과를 내는가 (키 불필요).

    픽스처는 의도적으로 레거시 단일 파일이라 그 경로는 [1]~[3]이 계속 검증한다.
    여기서는 같은 규칙을 저장소 포맷으로 옮겨 CLI를 다시 돌려, 포맷 전환이
    지적을 바꾸지 않는지 확인한다. 기존 리포를 깨지 않는다는 약속의 증거.
    """
    legacy = load_conventions(workdir / "conventions.yml")
    root = workdir / "conventions"
    for rule in legacy:
        write_rule(root, "active", StoredRule(**rule.model_dump()))

    scope = collect_diff(workdir)
    from_legacy = check_scope(scope, legacy)
    from_store = check_scope(scope, load_conventions(root))

    key = lambda fs: sorted((f.file, f.line, f.evidence.rule_id, f.title) for f in fs)  # noqa: E731
    if key(from_legacy) != key(from_store):
        return StepResult(
            "[4] 저장소 포맷 등가성", "fail",
            f"레거시 {len(from_legacy)}건 vs 저장소 {len(from_store)}건 — 지적이 달라짐",
        )

    # 승인 대기 후보는 적용되지 않아야 한다 (승인 게이트가 실제로 게이트인가)
    write_rule(root, "candidate", StoredRule(**legacy[0].model_dump(), reason="검증용 후보"))
    if len(check_scope(scope, load_conventions(root))) != len(from_store):
        return StepResult(
            "[4] 저장소 포맷 등가성", "fail", "candidates/의 규칙이 리뷰에 적용됨",
        )
    shutil.rmtree(root, ignore_errors=True)
    return StepResult(
        "[4] 저장소 포맷 등가성", "pass",
        f"두 포맷 모두 {len(from_store)}건 동일, 미승인 후보는 미적용",
    )


def _fixture_expected() -> list[learn_scoring.ExpectedRule]:
    """정답지(fixture/conventions.yml)의 규칙을 채점용 ExpectedRule로 변환."""
    answer = load_conventions(FIXTURE / "conventions.yml")
    return [
        learn_scoring.ExpectedRule(r.category, r.facet, r.value, source="fixture/conventions.yml")
        for r in answer
        if r.facet in ("prefix", "suffix", "casing")
    ]


LEARN_SAMPLES = 3  # 같은 입력 반복 횟수 — 재현성(flap) 측정 (verification-plan 측정지표)


def step_learn_quality(workdir: Path) -> tuple[StepResult, list[str]]:
    """[5] learn 품질: fixture 통계로 learn을 돌려 채택 규칙을 정답지와 대조.

    두 가지를 함께 잰다:
    - 정확도: 채택 규칙의 recall/precision (정답지 = fixture/conventions.yml)
    - 재현성: 같은 입력을 LEARN_SAMPLES회 반복해 규칙셋이 얼마나 흔들리는지(flap).
      learn 온도가 제품 기본값(openai=1.0)이라 작은 입력에서 특히 흔들린다 —
      단발 채점은 오해를 부르므로 평균과 flap을 같이 남긴다.

    픽스처는 식별자가 6/6/2개뿐이라 MIN_RULE_OCCURRENCES(20) 게이트에 전부 걸린다.
    그대로 돌리면 정답과 무관하게 0규칙이 나오므로, 분포를 보존한 채 통계를 게이트
    위로 스케일해(learn_scoring.scale_to_gate) LLM의 (category,facet,value) 식별력을
    잰다. 게이트 자체는 키 없는 결정적 단계에서 이미 검증된다.
    """
    name = "[5] learn 품질 검증"
    if not HAS_KEY:
        return StepResult(name, "skipped", f"{KEY_ENV} 없음"), []

    expected = _fixture_expected()
    # 깨끗한 픽스처의 통계를 쓴다 — workdir은 주입 위반이 섞여 관행이 게이트 아래로
    # 내려가므로(그건 [1]이 재현할 대상이다), learn 품질은 정답지가 기술하는 원본
    # 픽스처에서 재야 한다.
    stats = extract_stats(FIXTURE)
    scaled, factor = learn_scoring.scale_to_gate(stats, MIN_RULE_OCCURRENCES)
    model = default_learn_model()

    samples: list[tuple[learn_scoring.Score, learn_scoring.LearnRun]] = []
    errors: list[str] = []
    for _ in range(LEARN_SAMPLES):
        try:
            run = learn_scoring.learn_with_usage(scaled, model=model)
        except Exception as exc:  # 재시도까지 소진한 실패 — 기록하고 계속
            errors.append(str(exc))
            continue
        samples.append((learn_scoring.score_rules(run.rules, expected), run))

    if not samples:
        return StepResult(name, "fail", f"learn {LEARN_SAMPLES}회 모두 실패: {errors[-1]}"), []

    recalls = [s.recall for s, _ in samples]
    precisions = [s.precision for s, _ in samples]
    mean_recall = sum(recalls) / len(recalls)
    mean_precision = sum(precisions) / len(precisions)
    distinct_rulesets = {tuple(s.adopted) for s, _ in samples}
    flap = len(distinct_rulesets)  # 1이면 안정, >1이면 흔들림
    total_tok = sum(r.total_tokens for _, r in samples)
    costs = [r.cost_usd for _, r in samples if r.cost_usd is not None]
    total_cost = sum(costs) if costs else None

    ok = mean_recall >= PASS_LINE and mean_precision >= PASS_LINE
    detail = (
        f"recall 평균 {mean_recall:.0%} {[f'{r:.0%}' for r in recalls]}, "
        f"precision 평균 {mean_precision:.0%}; flap {flap}종/{len(samples)}회 "
        f"({'안정' if flap == 1 else '흔들림'}); {model} ×{len(samples)}, {total_tok} tok"
        + (f", ≈${total_cost:.4f}" if total_cost is not None else "")
        + (f"; 실패 {len(errors)}회" if errors else "")
    )

    section = [
        "## [5] learn 품질 상세",
        "",
        f"- 모델: `{model}` (provider {PROVIDER}) / 통계 ×{factor} 스케일 "
        f"(픽스처가 게이트 {MIN_RULE_OCCURRENCES} 미만이라 분포 보존 후 표본 확대)",
        f"- 정답지: fixture/conventions.yml ({len(expected)}개 규칙)",
        f"- recall 평균 {mean_recall:.0%} / precision 평균 {mean_precision:.0%} "
        f"({len(samples)}회 샘플)",
        f"- 재현성(flap): 서로 다른 규칙셋 {flap}종 → "
        f"{'같은 결과로 안정' if flap == 1 else '입력이 같아도 결과가 흔들림 (learn 기본 온도)'}",
    ]
    if errors:
        section.append(f"- ⚠️ 파싱 실패 {len(errors)}회 (재시도 소진): {errors[-1][:120]}")
    section += [
        "",
        "| 샘플 | recall | precision | 채택 규칙 | tok |",
        "|---|---|---|---|---|",
    ]
    for i, (score, run) in enumerate(samples, 1):
        section.append(
            f"| {i} | {score.recall:.0%} | {score.precision:.0%} | "
            f"{score.adopted or '없음'} | {run.total_tokens} |"
        )
    return StepResult(name, "pass" if ok else "fail", detail), section


def step_model_comparison(workdir: Path) -> tuple[StepResult, list[str]]:
    """[6] 모델 비교: 같은 fixture 통계를 활성 provider의 tier들로 판정시켜
    규칙 정확도·토큰·비용을 비교 → 설계 문서 §4 모델 전략 판정.

    anthropic이면 haiku/sonnet/opus(§4의 판단 대상)로 돈다 — 단 실제
    ANTHROPIC_API_KEY가 있어야 한다. 현재처럼 provider가 openai면 openai tier를
    비교하고, anthropic 판정은 키가 필요하다는 사실을 채점표에 남긴다.
    픽스처는 통제된(깨끗한) 입력이라 tier 간 차이는 작게 나오는 게 정상 —
    지저분한 실제 데이터의 tier 차이는 score_real_repos.py가 잰다.
    """
    name = "[6] 모델 비교"
    if not HAS_KEY:
        return StepResult(name, "skipped", f"{KEY_ENV} 없음"), []

    tiers = learn_scoring.MODEL_TIERS.get(PROVIDER, [])
    if not tiers:
        return StepResult(name, "skipped", f"provider {PROVIDER}: tier 목록 미정의"), []

    expected = _fixture_expected()
    stats = extract_stats(FIXTURE)  # 깨끗한 픽스처 ([5]와 동일한 이유)
    scaled, factor = learn_scoring.scale_to_gate(stats, MIN_RULE_OCCURRENCES)

    cost = learn_scoring.CostLog()
    rows: list[tuple[str, str, learn_scoring.Score | None, learn_scoring.LearnRun | None, str]] = []
    for label, model in tiers:
        try:
            run = learn_scoring.learn_with_usage(scaled, model=model)
            score = learn_scoring.score_rules(run.rules, expected)
            cost.add(f"{label} ({model})", run)
            rows.append((label, model, score, run, ""))
        except Exception as exc:
            rows.append((label, model, None, None, str(exc)))

    errored = [r for r in rows if r[4]]
    summary = ", ".join(
        f"{label} recall {score.recall:.0%}" if score else f"{label} 실패"
        for label, _, score, _, _ in rows
    )
    caveat = ""
    if PROVIDER != "anthropic":
        caveat = " · §4 원안은 anthropic haiku/sonnet/opus (동종 비교)"
    total_usd = cost.total_usd
    detail = (
        f"{PROVIDER} {len(tiers)} tier — {summary} "
        f"({cost.total_tokens} tok"
        + (f", ≈${total_usd:.4f}" if total_usd is not None else "")
        + ")"
        + caveat
    )

    section = [
        "## [6] 모델 비교 상세",
        "",
        f"- provider: {PROVIDER} / 통계 스케일 ×{factor} / 정답지 fixture ({len(expected)}개)",
    ]
    if PROVIDER != "anthropic":
        section.append(
            f"- 참고: 설계 문서 §4의 원래 판단 대상은 anthropic haiku/sonnet/opus다. "
            f"활성 provider가 {PROVIDER}라 그에 대응하는 {PROVIDER} 3개 모델로 동종 비교했다. "
            "anthropic tier를 직접 재려면 유효한 ANTHROPIC_API_KEY로 재실행하면 된다."
        )
    section += [
        "",
        "| tier | 모델 | recall | precision | in tok | out tok | ≈USD | 놓침 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for label, model, score, run, err in rows:
        if score is None:
            section.append(f"| {label} | `{model}` | — | — | — | — | — | 실패: {err} |")
            continue
        usd = run.cost_usd
        section.append(
            f"| {label} | `{model}` | {score.recall:.0%} | {score.precision:.0%} | "
            f"{run.input_tokens} | {run.output_tokens} | "
            + (f"${usd:.4f}" if usd is not None else "—")
            + f" | {score.missed or '없음'} |"
        )
    section += ["", *cost.as_markdown()]

    status = "fail" if errored else "pass"
    return StepResult(name, status, detail), section


def step_review_llm_e2e(workdir: Path) -> StepResult:
    """[7] (키 필요 — 스켈레톤) 리뷰 LLM triage e2e: `pumpkins --repo <workdir>`
    (LLM 켬)를 1회 실행해 triage·extra findings·토큰 사용량을 기록."""
    if not HAS_KEY:
        return StepResult("[7] 리뷰 LLM triage e2e", "skipped", f"{KEY_ENV} 없음")
    return StepResult("[7] 리뷰 LLM triage e2e", "skipped", "스켈레톤 — 아직 미구현 (키 확보 후 작업)")


# ------------------------------------------------------------------ 채점표

def write_scorecard(
    steps: list[StepResult], metrics: dict, extra_sections: list[list[str]]
) -> Path:
    RESULTS.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    path = RESULTS / f"scorecard-{now.strftime('%Y%m%d-%H%M%S')}.md"
    lines = [
        "# pumpkins 자기 검증 채점표",
        "",
        f"- 실행: {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"- LLM 프로바이더: {PROVIDER} / API 키({KEY_ENV}): "
        f"{'있음' if HAS_KEY else '없음 (키 필요 단계는 skip)'}",
        f"- 통과선: recall/precision ≥ {PASS_LINE:.0%} (fixture 통제 환경)",
        "",
        "| 단계 | 결과 | 상세 |",
        "|---|---|---|",
    ]
    for s in steps:
        icon = {"pass": "✅", "fail": "❌", "skipped": "⏭️"}[s.status]
        lines.append(f"| {s.name} | {icon} {s.status} | {s.detail} |")
    lines += [
        "",
        "## [1] 주입 상세",
        "",
        f"- 주입 위반: {len([i for i in INJECTIONS if i.expected])}건, "
        f"무해 변경: {len([i for i in INJECTIONS if not i.expected])}건",
        f"- recall: {metrics['recall']:.0%} / precision: {metrics['precision']:.0%}",
        f"- missed: {metrics['missed'] or '없음'}",
        f"- false positives: {metrics['false_positives'] or '없음'}",
    ]
    for section in extra_sections:
        lines += ["", *section]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _force_utf8_streams() -> None:
    """cp949 등 비-UTF-8 콘솔에서도 이모지/기호 출력이 깨지지 않게 스트림을 UTF-8로."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def main() -> int:
    _force_utf8_streams()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep-workdir", action="store_true", help="검증용 임시 레포를 남겨둠")
    args = ap.parse_args()
    setup_logging(False)

    workdir = setup_workdir(args.keep_workdir)
    try:
        step1, metrics = step_checker_injection(workdir)
        learn_step, learn_section = step_learn_quality(workdir)
        model_step, model_section = step_model_comparison(workdir)
        steps = [
            step1,
            step_cli_smoke(workdir),
            step_scope_isolation(workdir),
            step_store_format_equivalence(workdir),
            learn_step,
            model_step,
            step_review_llm_e2e(workdir),
        ]
        extra_sections = [s for s in (learn_section, model_section) if s]
    finally:
        if not args.keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    scorecard = write_scorecard(steps, metrics, extra_sections)
    print()
    for s in steps:
        icon = {"pass": "✅", "fail": "❌", "skipped": "⏭️"}[s.status]
        print(f"{icon} {s.name}: {s.detail}")
    print(f"\n채점표: {scorecard}")

    return 0 if all(s.status != "fail" for s in steps) else 1


if __name__ == "__main__":
    sys.exit(main())
