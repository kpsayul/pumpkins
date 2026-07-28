#!/usr/bin/env python3
"""learn 품질을 '정답이 공개된' 실제 오픈소스 리포로 채점한다.

배경 (docs/verification-plan.md "픽스처 검증의 한계"): 자체 픽스처가
recall/precision 100%인 상태에서 실제 리포 두 곳에 돌렸더니 학습된 규칙이 양쪽 다
틀렸다. 통제 환경 검증만으로는 부족하다는 게 이 스크립트의 출발점이고, "정답이
공개된 리포로 채점"이 사람 판단 없이 채점 가능한 유일한 축이다.

스타일 가이드가 문서로 존재하는 리포에 learn을 돌려, 문서화된 스타일을 정답지로
삼아 채택 규칙의 정확도를 잰다:

  - fmt        : CONTRIBUTING.md — Google C++ Style, 단 함수/타입은 snake_case
  - googletest : CONTRIBUTING.md — Google C++ Style (멤버 트레일링 `_`, 함수/타입 UpperCamel)
  - Catch2     : 명문 네이밍 문서 없음 — de-facto (m_ 멤버 / lowerCamel 함수 / UpperCamel 타입)

리포는 얕게(depth 1) 작업 디렉터리에 클론하며 pumpkins 리포에는 절대 커밋하지 않는다.
API 키가 필요하고, 모델 호출의 토큰/비용을 로그와 채점표에 남긴다.

사용:
    python verification/score_real_repos.py                 # 기본 learn 모델로 3개 리포 채점
    python verification/score_real_repos.py --compare-models # provider의 모든 tier로 비교(호출 다수)
    python verification/score_real_repos.py --clone-dir DIR  # 기존 클론 재사용(재클론 방지)
    python verification/score_real_repos.py --keep-clones    # 임시 클론을 지우지 않음

채점표는 verification/results/ 에 남는다.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from pumpkins.config import (
    MIN_RULE_CONSISTENCY,
    MIN_RULE_OCCURRENCES,
    current_provider,
    default_learn_model,
    has_api_key,
    required_key_env,
    setup_logging,
)
from pumpkins.conventions import extract_stats

import learn_scoring  # flat import: verification/ 이 sys.path[0]

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"

load_dotenv(find_dotenv(usecwd=True))
PROVIDER = current_provider()
KEY_ENV = required_key_env()

log = logging.getLogger("score_real_repos")


# --------------------------------------------------------------- 대상 리포·정답지

@dataclass
class RealRepo:
    name: str
    clone_url: str
    include: list[str]                       # 라이브러리 소스로 스캔 범위 좁히기
    style_source: str                        # 정답지의 근거(문서 인용)
    expected: list[learn_scoring.ExpectedRule]
    notes: str = ""


REPOS: list[RealRepo] = [
    RealRepo(
        name="fmt",
        clone_url="https://github.com/fmtlib/fmt.git",
        include=["include/**", "src/**"],
        style_source="CONTRIBUTING.md: Google C++ Style Guide, 단 함수/타입 이름은 snake_case",
        expected=[
            learn_scoring.ExpectedRule(
                "function", "casing", "lower_snake",
                "fmt CONTRIBUTING: snake_case for function names",
            ),
            learn_scoring.ExpectedRule(
                "class_type", "casing", "lower_snake",
                "fmt CONTRIBUTING: snake_case for type names",
            ),
            learn_scoring.ExpectedRule(
                "member_variable", "casing", "lower_snake",
                "Google base: variables are snake_case",
            ),
        ],
        notes="깨끗한 snake_case 양성 케이스 — learn이 재현해야 정상.",
    ),
    RealRepo(
        name="googletest",
        clone_url="https://github.com/google/googletest.git",
        include=["googletest/include/**", "googletest/src/**"],
        style_source="CONTRIBUTING.md: Google C++ Style Guide (google/styleguide)",
        expected=[
            learn_scoring.ExpectedRule(
                "function", "casing", "UpperCamel",
                "Google style: function names UpperCamelCase",
            ),
            learn_scoring.ExpectedRule(
                "class_type", "casing", "UpperCamel",
                "Google style: type names UpperCamelCase",
            ),
            learn_scoring.ExpectedRule(
                "member_variable", "suffix", "_",
                "Google style: class data members have a trailing underscore",
            ),
        ],
        notes="멤버 트레일링 `_`는 struct 공개 멤버가 섞여 관측 커버리지가 게이트에 못 미칠 수 있음.",
    ),
    RealRepo(
        name="Catch2",
        clone_url="https://github.com/catchorg/Catch2.git",
        include=["src/**"],
        style_source="명문 네이밍 문서 없음 — de-facto 관행(관측 지배 패턴 + 커뮤니티 통용)",
        expected=[
            learn_scoring.ExpectedRule(
                "member_variable", "prefix", "m_",
                "Catch2 de-facto: private members use m_ prefix",
            ),
            learn_scoring.ExpectedRule(
                "function", "casing", "lowerCamel",
                "Catch2 de-facto: functions lowerCamel",
            ),
            learn_scoring.ExpectedRule(
                "class_type", "casing", "UpperCamel",
                "Catch2 de-facto: types UpperCamel",
            ),
        ],
        notes="다양성/오염 내구성 케이스 — 관행이 게이트(85%)를 못 넘으면 learn이 안전하게 기각하는 게 정답.",
    ),
]


# ------------------------------------------------------------------- 리포 준비

def ensure_clone(repo: RealRepo, clone_dir: Path) -> Path:
    """리포를 얕게 클론(또는 기존 것 재사용). 작업 디렉터리에만 둔다."""
    dest = clone_dir / repo.name
    if (dest / ".git").is_dir():
        log.info("%s: 기존 클론 재사용 (%s)", repo.name, dest)
        return dest
    log.info("%s: git clone --depth 1 %s", repo.name, repo.clone_url)
    proc = subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet", repo.clone_url, str(dest)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{repo.name} clone 실패: {proc.stderr.strip()}")
    return dest


# --------------------------------------------------------------------- 채점

@dataclass
class RepoResult:
    repo: RealRepo
    per_model: list[tuple[str, learn_scoring.Score, learn_scoring.LearnRun]] = field(default_factory=list)
    coverage: list[tuple[learn_scoring.ExpectedRule, int, int, float]] = field(default_factory=list)
    error: str = ""


def score_repo(repo: RealRepo, path: Path, models: list[str]) -> RepoResult:
    result = RepoResult(repo=repo)
    stats = extract_stats(path, include=repo.include)
    if sum(s.total for s in stats) == 0:
        result.error = "스캔된 식별자 0 — include 범위 확인 필요"
        return result

    # 정답 규칙별 관측 커버리지 — learn이 왜 채택/기각했는지 근거로 남긴다.
    for exp in repo.expected:
        count, denom, frac = learn_scoring.observed_coverage(
            stats, exp.category, exp.facet, exp.value
        )
        result.coverage.append((exp, count, denom, frac))

    for model in models:
        try:
            run = learn_scoring.learn_with_usage(stats, model=model)
        except Exception as exc:
            log.error("%s / %s: learn 실패: %s", repo.name, model, exc)
            result.per_model.append(
                (model, learn_scoring.Score([], [], [], [], [], 0.0, 1.0),
                 learn_scoring.LearnRun(model=model, rules=[], input_tokens=0, output_tokens=0, error=str(exc)))
            )
            continue
        score = learn_scoring.score_rules(run.rules, repo.expected)
        result.per_model.append((model, score, run))
        log.info(
            "%s / %s: recall %.0f%% precision %.0f%% (%d tok)",
            repo.name, model, score.recall * 100, score.precision * 100, run.total_tokens,
        )
    return result


# --------------------------------------------------------------------- 채점표

def write_scorecard(results: list[RepoResult], models: list[str]) -> Path:
    RESULTS.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    path = RESULTS / f"real-repos-{now.strftime('%Y%m%d-%H%M%S')}.md"
    cost = learn_scoring.CostLog()

    lines = [
        "# learn 품질 — 실제 오픈소스 리포 채점표",
        "",
        f"- 실행: {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"- provider: {PROVIDER} / 모델: {', '.join(f'`{m}`' for m in models)}",
        f"- 게이트: 채택 규칙은 occurrences ≥ {MIN_RULE_OCCURRENCES} AND "
        f"coverage ≥ {MIN_RULE_CONSISTENCY:.0%}",
        "- 정답지: 각 리포의 문서화된(또는 de-facto) 스타일. recall = 정답 규칙 중 "
        "learn이 채택한 비율, precision = 정답이 정의한 축에 올린 규칙 중 값이 맞은 비율.",
        "",
        "## 요약",
        "",
        "| 리포 | 모델 | recall | precision | 놓친 규칙 |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        if r.error:
            lines.append(f"| {r.repo.name} | — | — | — | 오류: {r.error} |")
            continue
        for model, score, run in r.per_model:
            if run.error:
                lines.append(f"| {r.repo.name} | `{model}` | — | — | 실패: {run.error} |")
                continue
            cost.add(f"{r.repo.name} / {model}", run)
            lines.append(
                f"| {r.repo.name} | `{model}` | {score.recall:.0%} | "
                f"{score.precision:.0%} | {score.missed or '없음'} |"
            )

    # 리포별 상세 — 정답지 근거 + 관측 커버리지(왜 채택/기각인지) + 채택 규칙.
    for r in results:
        lines += ["", f"## {r.repo.name}", "", f"- 정답지 근거: {r.repo.style_source}"]
        if r.repo.notes:
            lines.append(f"- 메모: {r.repo.notes}")
        if r.error:
            lines.append(f"- ⚠️ {r.error}")
            continue
        lines += [
            "",
            "정답 규칙과 관측 커버리지 (게이트 미달이면 learn이 채택하지 않는 게 정상):",
            "",
            "| 정답 규칙 (category/facet=value) | 관측 | 게이트 통과? | 근거 |",
            "|---|---|---|---|",
        ]
        for exp, count, denom, frac in r.coverage:
            passes = "예" if (denom >= MIN_RULE_OCCURRENCES and frac >= MIN_RULE_CONSISTENCY) else "아니오"
            lines.append(
                f"| {exp.category}/{exp.facet}={exp.value} | "
                f"{count}/{denom} ({frac:.0%}) | {passes} | {exp.source} |"
            )
        for model, score, run in r.per_model:
            if run.error:
                continue
            lines += [
                "",
                f"### {r.repo.name} × `{model}`",
                f"- recall {score.recall:.0%} / precision {score.precision:.0%} "
                f"(in {run.input_tokens} / out {run.output_tokens} tok"
                + (f" / ≈${run.cost_usd:.4f}" if run.cost_usd is not None else "")
                + ")",
                f"- 채택: {score.adopted or '없음'}",
                f"- 일치: {score.matched or '없음'}",
                f"- 놓침: {score.missed or '없음'}",
                f"- 오채택(정답 축, 값 불일치): {score.wrong or '없음'}",
            ]

    lines += ["", "## 비용", "", *cost.as_markdown()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------- main

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
    ap.add_argument("--compare-models", action="store_true",
                    help="provider의 모든 tier로 비교 (기본: learn 기본 모델 하나)")
    ap.add_argument("--clone-dir", type=Path, default=None,
                    help="클론 위치 (기본: 임시 디렉터리). 기존 클론이 있으면 재사용")
    ap.add_argument("--keep-clones", action="store_true", help="임시 클론을 지우지 않음")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    setup_logging(args.verbose)

    if not has_api_key():
        log.error("%s 없음 (LLM_PROVIDER=%s) — 실제 리포 learn 채점은 키가 필요합니다",
                  KEY_ENV, PROVIDER)
        return 2

    if args.compare_models:
        models = [m for _, m in learn_scoring.MODEL_TIERS.get(PROVIDER, [])]
        if not models:
            log.error("provider %s: tier 목록 미정의", PROVIDER)
            return 2
    else:
        models = [default_learn_model()]

    clone_dir = args.clone_dir or Path(tempfile.mkdtemp(prefix="pumpkins-realrepos-"))
    clone_dir.mkdir(parents=True, exist_ok=True)
    log.info("클론 위치: %s%s", clone_dir, "" if args.keep_clones or args.clone_dir else " (자동 삭제)")

    results: list[RepoResult] = []
    try:
        for repo in REPOS:
            try:
                path = ensure_clone(repo, clone_dir)
            except Exception as exc:
                log.error("%s", exc)
                results.append(RepoResult(repo=repo, error=str(exc)))
                continue
            results.append(score_repo(repo, path, models))
    finally:
        if not args.keep_clones and not args.clone_dir:
            shutil.rmtree(clone_dir, ignore_errors=True)

    scorecard = write_scorecard(results, models)

    print()
    for r in results:
        if r.error:
            print(f"❌ {r.repo.name}: {r.error}")
            continue
        for model, score, run in r.per_model:
            tag = "❌ 실패" if run.error else f"recall {score.recall:.0%} / precision {score.precision:.0%}"
            print(f"• {r.repo.name} × {model}: {tag}")
    print(f"\n채점표: {scorecard}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
