# 확장 가이드

## 새 규칙 프로파일 추가 (버그 클래스 또는 컨벤션)

프로파일 하나 = "이 종류의 지적을 하겠다"는 단위입니다. 기능적 버그 클래스뿐 아니라
**프로젝트 컨벤션**(이 아이템의 핵심 방향)도 같은 방식으로 프로파일로 얹습니다.
[analysis/checks.py](../src/pumpkins/analysis/checks.py)의 `CHECK_PROFILES`에 키 하나 추가하면
CLI `--profile` 선택지가 자동 생성됩니다.

```python
CHECK_PROFILES: dict[str, list[str]] = {
    "concurrency": [...],
    # 예: 메모리 안전성 프로파일 (clang-tidy 기반)
    "memory": [
        "bugprone-use-after-move",
        "bugprone-dangling-handle",
        "clang-analyzer-cplusplus.NewDelete*",
        "clang-analyzer-cplusplus.Move",
    ],
    # 예: 컨벤션 프로파일 — clang-tidy readability-identifier-naming으로
    #     일부 규칙은 기계적 확인이 가능하지만, "기존 코드의 관행 추론"은
    #     결국 LLM 프롬프트 쪽이 담당한다 (아래 주의 참고).
    "convention": [
        "readability-identifier-naming",
    ],
}
```

원칙:

- **좁고 정밀하게.** clang-tidy 목록은 false positive가 적은 체크 위주로. 넓은 판단(특히 명문화 안 된 관행)은 LLM 쪽이 담당.
- `clang-analyzer-*` 체크는 컴파일 플래그 의존도가 높아 **얕은 모드에서 특히 부정확** — 넣는다면 compile-DB 모드 검증부터.
- 체크 이름은 clang-tidy 버전에 따라 다름: `clang-tidy --list-checks -checks='*' | grep <keyword>`로 확인.

> **컨벤션 프로파일의 핵심은 clang-tidy가 아니라 LLM입니다.** `readability-identifier-naming`은
> 규칙을 사람이 명시해줘야 동작하지만, 이 아이템이 노리는 건 *"리포 기존 코드에서 관행을 읽어내
> 어긋난 곳을 짚는"* 것 — 구체적인 구현 방향(사전 학습형 `conventions/` 저장소 등)은
> [convention-detection-design.md](convention-detection-design.md)에 확정되어 있습니다.

프로파일에 맞춰 **LLM 프롬프트도 갱신**해야 합니다 — [llm/postprocess.py](../src/pumpkins/llm/postprocess.py)의
`_SYSTEM_PROMPT` task 3 목록이 concurrency 전용으로 하드코딩되어 있으므로, 프로파일이 늘어나면
프로파일별 프롬프트 조각(dict)으로 분리하는 리팩토링을 먼저 하세요.

> **이 리팩토링이 현재 1순위입니다.** fmt PR #4865에 돌렸을 때 LLM 출력이 11토큰이었습니다 —
> 프롬프트가 동시성만 찾도록 고정돼 있고 그 PR엔 동시성 코드가 없었으니, 모델은 지시를 정확히
> 따른 것입니다. 정작 그 PR의 실제 결함은 C++11을 지원하는 리포에 C++17 `inline` 변수를 넣어
> CI를 깨뜨리는 것이었고, 이건 어떤 프로파일에도 속하지 않습니다.
> 근거: [설계 문서 §6 "다음에 할 일"](convention-detection-design.md). (컨벤션 프로파일이라면
"diff 주변의 기존 코드에서 명명·구조 관행을 먼저 추론한 뒤, 변경분이 그걸 어겼는지 판정하라"는
지시가 이 자리에 들어갑니다.)

## 새 diff 소스 추가 (예: GitHub PR)

파싱(`parse_diff_text`)은 git 호출과 분리된 순수 함수입니다. PR diff를 지원하려면:

```python
# diff/collector.py에 추가
def collect_pr_diff(diff_text: str, base_ref: str) -> DiffScope:
    """GitHub API 등에서 받은 unified diff 텍스트를 그대로 파싱."""
    files, skipped = parse_diff_text(diff_text)
    return DiffScope(base_ref=base_ref, files=files, skipped_files=skipped)
```

`gh pr diff <num>` 출력이나 `Accept: application/vnd.github.diff` API 응답을 그대로 넣으면 됩니다.
`skipped_files`(C++ 확장자가 아니어서 탈락한 경로)를 **반드시 같이 넘기세요** — 리포트가 "안 봤다"고
말할 근거이고, 이게 빠지면 finding 0건이 통과로 읽힙니다.

## 새 분석기 추가 (clang-tidy 외)

Stage 2의 계약은 `run(scope: DiffScope) -> list[RawDiagnostic]` 하나입니다.
같은 시그니처의 러너를 만들어 `cli.run_pipeline`에서 결과를 이어붙이면 됩니다 (예: cppcheck, libclang AST 워커).
`RawDiagnostic.check`에 분석기 접두사를 붙여 출처를 구분하세요 (`cppcheck-nullPointer` 등).

**분석하지 못한 파일은 러너가 노출해야 합니다** — `ClangTidyRunner`가 `skipped_headers`/`analyzed_files`를
공개하는 것과 같은 방식으로. 조용히 건너뛰면 리포트가 그 파일을 검사한 것처럼 보입니다.

새 분석기의 finding에는 **`Evidence`가 필수**입니다. `DetectorKind`에 항목을 추가하고,
같은 입력에 같은 답을 내면 `DETERMINISTIC_DETECTORS`에도 넣으세요 — 그 집합이 CI를 막을 수 있는
유일한 범위이므로, 확신이 없으면 넣지 마세요(안전한 실패 방향은 "재현 안 됨"입니다).
결정적 분석기라도 **결과가 모델을 거쳐 걸러진다면 `reproducible=False`로 덮어써야 합니다** —
clang-tidy 진단이 LLM triage를 통과한 경우가 그 예입니다.

## 리포트 포맷 추가

Stage 4의 계약은 `render_*(result: ReviewResult) -> str`입니다.
`report/` 아래에 `sarif.py`(GitHub code scanning용), `json.py` 등을 추가하고 CLI에 `--format` 옵션을 붙이면 됩니다.

새 포맷도 **`result.has_coverage_gap`을 반드시 반영해야 합니다.** 커버리지 공백이 있는데 통과 신호를
내보내면 CI가 잘못된 초록불을 켭니다 — SARIF라면 공백을 별도 notification으로 올리세요.

## 실행 산출물에 항목 추가 (`report/dump.py`)

`--out-dir`이 쓰는 파일 목록은 `_ARTIFACTS`에 있고, 쓰기 전에 그 목록만 지웁니다.
**새 산출물을 추가하면 `_ARTIFACTS`에도 넣으세요** — 빠뜨리면 이전 실행의 파일이 남아
디버깅 중 잘못된 귀인을 유발합니다(없는 파일보다 낡은 파일이 위험합니다).

파이프라인이 본 원본 입력이 필요하면 `RunContext`에 필드를 추가하고 `cli.run_pipeline`에서
채우세요. **`ReviewResult`에 넣지 마세요** — 그건 리포트의 계약이고, 프롬프트·패치는
디버깅 재료지 리뷰 출력이 아닙니다.

`run.json`에 필드를 더할 때는 "모델을 바꿨을 때 차이의 원인을 가릴 수 있는가"를 기준으로
판단하세요. 그게 이 파일의 존재 이유입니다 (프로바이더/모델, clang-tidy 버전,
`rules_fingerprint`가 그 세 축입니다).

## 규칙 적용 범위 (`conventions/scope.py`)

`RuleScope`의 계약은 `applies_to(path: str) -> bool` 하나입니다. 새 스코프 축(예: 파일 크기, 소유
팀, git 이력상 최근 수정일)을 넣으려면 필드를 추가하고 `applies_to`에 조건을 더하면 됩니다. 두 가지를
지키세요:

- **매칭은 결정적이어야 합니다** — 리포 상대 POSIX 경로 + `fnmatchcase`. 플랫폼에 따라 결과가
  달라지면 같은 규칙 저장소가 CI와 로컬에서 다른 지적을 냅니다.
- **`describe()`도 같이 갱신하세요** — 지적 코멘트에 적용 범위가 표시되지 않으면 읽는 사람이
  "이 규칙이 여기 적용되는 게 맞나"를 판단할 수 없습니다.

`learn` 쪽 스캔 범위(`extractor.select_files`)와 규칙 쪽 판정 범위가 같은 어휘를 공유하는 것이
설계 의도입니다 — 좁혀 학습한 규칙에는 그 범위가 자동으로 박힙니다.

## 규칙에 필드 추가 (`conventions/store.py`)

규칙 스키마는 두 층입니다. 어디에 넣을지가 중요합니다.

| 모델 | 용도 | 넣어야 할 것 |
|---|---|---|
| `ConventionRule` (learner.py) | **LLM의 출력 스키마이기도 함** | 모델이 채워야 하는 것만 (facet/value/근거 수치 등) |
| `StoredRule` (store.py) | 디스크 표현 | 사람이나 reconcile만 채우는 것 (`reason`, `learned_at`, 결정 메타데이터) |

LLM이 채우면 안 되는 필드를 `ConventionRule`에 넣으면 모델이 그 값을 지어냅니다.
`scope`가 그 예라서, 스키마에는 있지만 프롬프트가 "비워 두라"고 지시하고 코드가 스캔 범위로 덮어씁니다.

새 필드가 재실행 시 보존돼야 한다면 `reconcile`도 같이 고쳐야 합니다 — 갱신 경로가
`stored.reason = previous.reason`처럼 명시적으로 **이전 값을 이어받게** 되어 있습니다.
빼먹으면 learn을 다시 돌릴 때 조용히 초기화되고, 그게 이 모듈이 애초에 고친 결함입니다.

상태를 늘리려면(`deprecated` 같은 것) `config.RULE_STATUS_DIRS`에 항목을 추가하세요 —
디렉터리가 곧 상태이므로 그 dict가 유일한 정의입니다.

## LLM 관련 조정 포인트

| 조정 | 위치 | 비고 |
|---|---|---|
| 모델 변경 | CLI `--model` 또는 `config.DEFAULT_REVIEW_MODEL` / `DEFAULT_LEARN_MODEL` | 리뷰 `claude-opus-4-8`, 학습 `claude-sonnet-5` — 단계별 근거는 [설계 문서 §4](convention-detection-design.md) |
| 프롬프트 | `llm/postprocess.py` `_SYSTEM_PROMPT` | 실패 시나리오를 구체적으로 쓰게 하는 문구가 핵심 |
| 샘플링 온도 | `config.REVIEW_TEMPERATURE` / `LEARN_TEMPERATURE` | 리뷰는 `0.0` 고정(사용자에게 바로 가는 출력), 학습은 `None`(프로바이더 기본값 — 임계선·승인 게이트가 뒤에 있음). `None`은 파라미터를 **아예 보내지 않는다**는 뜻 — 일부 모델이 이 파라미터를 거부하므로 |
| 출력 스키마 | `_Verdict`, `_ExtraFinding`, `_LlmReview` | Pydantic 모델 수정만으로 스키마 강제 유지 |
| 대형 diff 청킹 | `process()` 호출 전 `DiffScope` 분할 | 파일 단위 분할 → 호출 병렬화 순서로 |

## 상수 (config.py)

| 상수 | 기본값 | 의미 |
|---|---|---|
| `LINE_FILTER_MARGIN` | 15 | 변경 범위 확장폭 — 동시성 문맥 확보용. 노이즈가 늘면 줄일 것 |
| `SHALLOW_MODE_STD` | c++17 | 얕은 모드 기본 표준. 대상 레포에 맞게 CLI 옵션화 가능 |
| `COMPILE_DB_CANDIDATES` | `.`, `build`, `out`, ... | compile_commands.json 탐색 경로 |
| `CPP_EXTENSIONS` | .cpp/.h 등 | diff에서 C++로 취급할 확장자. 여기 없는 확장자는 `skipped_files`로 빠져 리포트에 명시됨 |
| `MIN_RULE_OCCURRENCES` / `MIN_RULE_CONSISTENCY` | 20 / 0.85 | 컨벤션 채택 임계선. **분모가 맞을 때만 안전하다** — [설계 문서 §5.5](convention-detection-design.md) |
| `LEARN_SKIP_DIRS` | `third_party`, `vendor`, ... | learn이 절대 읽지 않는 디렉터리 |
| `LEARN_TEST_DIRS` | `test`, `tests`, ... | 기본 제외, `learn --include-tests`로 해제. 벤더링된 테스트 프레임워크가 통계를 뒤집은 실측이 근거 |
