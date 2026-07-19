#!/usr/bin/env python3
"""pumpkins 자기 검증 드라이버 — MVP 3단계 (verification-plan Track A 자동화).

전체 flow를 처음부터 끝까지 실행하되, API 키가 필요한 단계는 스켈레톤이라
자동으로 skip 처리된다 (키가 없어도 flow는 끝까지 돈다):

  [1] checker 주입 검증   (키 불필요 — 실제 실행)
      fixture 레포 복사 → git 초기화·커밋 → 위반 5건 + 무해 변경 3건 주입
      → conventions.yml 대조 → 주입 정답지와 비교해 recall/precision 산출
  [2] CLI 스모크          (키 불필요 — 실제 실행; clang-tidy 없으면 skip)
      `pumpkins --no-llm` 전체 파이프라인이 리포트까지 뽑는지 확인
  [3] learn 품질 검증     (키 필요 — 스켈레톤: fixture에 learn을 돌려 생성된
      conventions.yml을 정답지와 비교. 키 오면 여기만 채우면 됨)
  [4] 모델 비교           (키 필요 — 스켈레톤: 같은 통계를 sonnet/haiku/opus로
      판정시켜 규칙 정확도 비교 → 설계 문서 §4 모델 전략 확정)
  [5] 리뷰 LLM triage e2e (키 필요 — 스켈레톤)

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

from pumpkins.config import current_provider, has_api_key, required_key_env, setup_logging
from pumpkins.conventions import check_scope, load_conventions
from pumpkins.diff import collect_diff

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
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


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

    observed = {
        (f.check.removeprefix("convention:"), m.group(1))
        for f in findings
        if (m := re.match(r"`([^`]+)`", f.title))
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
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return StepResult("[2] CLI 스모크", "fail", proc.stderr.strip()[-300:])
    if "- conventions: 3 rule(s)" not in proc.stdout:
        return StepResult("[2] CLI 스모크", "fail", "report missing conventions header")
    n = proc.stdout.count("convention:")
    return StepResult("[2] CLI 스모크", "pass", f"report OK, convention finding {n}건 포함")


def step_learn_quality(workdir: Path) -> StepResult:
    """[3] (키 필요 — 스켈레톤) learn 품질: fixture에 `pumpkins learn`을 돌려
    생성된 conventions.yml을 정답지(fixture/conventions.yml)와 비교.

    키가 오면 채울 내용:
      1. `pumpkins learn --repo <workdir> --out <workdir>/learned.yml`
      2. load_conventions()로 양쪽 로드 → (category, facet, value) 집합 비교
      3. 규칙 recall/precision을 채점표에 기록
    """
    if not HAS_KEY:
        return StepResult("[3] learn 품질 검증", "skipped", f"{KEY_ENV} 없음")
    return StepResult("[3] learn 품질 검증", "skipped", "스켈레톤 — 아직 미구현 (키 확보 후 작업)")


def step_model_comparison(workdir: Path) -> StepResult:
    """[4] (키 필요 — 스켈레톤) 모델 비교: 같은 통계를 sonnet-5 / haiku-4-5 /
    opus-4-8로 각각 판정시켜 규칙 정확도·비용을 비교 → 설계 문서 §4 확정.

    키가 오면 채울 내용:
      for model in (sonnet, haiku, opus):
          ConventionLearner(model=model).learn(extract_stats(workdir))
      → 정답지 대비 정확도 + usage 토큰 비용 표 생성
    """
    if not HAS_KEY:
        return StepResult("[4] 모델 비교", "skipped", f"{KEY_ENV} 없음")
    return StepResult("[4] 모델 비교", "skipped", "스켈레톤 — 아직 미구현 (키 확보 후 작업)")


def step_review_llm_e2e(workdir: Path) -> StepResult:
    """[5] (키 필요 — 스켈레톤) 리뷰 LLM triage e2e: `pumpkins --repo <workdir>`
    (LLM 켬)를 1회 실행해 triage·extra findings·토큰 사용량을 기록."""
    if not HAS_KEY:
        return StepResult("[5] 리뷰 LLM triage e2e", "skipped", f"{KEY_ENV} 없음")
    return StepResult("[5] 리뷰 LLM triage e2e", "skipped", "스켈레톤 — 아직 미구현 (키 확보 후 작업)")


# ------------------------------------------------------------------ 채점표

def write_scorecard(steps: list[StepResult], metrics: dict) -> Path:
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
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep-workdir", action="store_true", help="검증용 임시 레포를 남겨둠")
    args = ap.parse_args()
    setup_logging(False)

    workdir = setup_workdir(args.keep_workdir)
    try:
        step1, metrics = step_checker_injection(workdir)
        steps = [
            step1,
            step_cli_smoke(workdir),
            step_learn_quality(workdir),
            step_model_comparison(workdir),
            step_review_llm_e2e(workdir),
        ]
    finally:
        if not args.keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    scorecard = write_scorecard(steps, metrics)
    print()
    for s in steps:
        icon = {"pass": "✅", "fail": "❌", "skipped": "⏭️"}[s.status]
        print(f"{icon} {s.name}: {s.detail}")
    print(f"\n채점표: {scorecard}")

    return 0 if all(s.status != "fail" for s in steps) else 1


if __name__ == "__main__":
    sys.exit(main())
