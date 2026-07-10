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
> 어긋난 곳을 짚는"* 것 — 그건 아래 프롬프트 확장으로 구현합니다.

프로파일에 맞춰 **LLM 프롬프트도 갱신**해야 합니다 — [llm/postprocess.py](../src/pumpkins/llm/postprocess.py)의
`_SYSTEM_PROMPT` task 3 목록이 concurrency 전용으로 하드코딩되어 있으므로, 프로파일이 늘어나면
프로파일별 프롬프트 조각(dict)으로 분리하는 리팩토링을 먼저 하세요. (컨벤션 프로파일이라면
"diff 주변의 기존 코드에서 명명·구조 관행을 먼저 추론한 뒤, 변경분이 그걸 어겼는지 판정하라"는
지시가 이 자리에 들어갑니다.)

## 새 diff 소스 추가 (예: GitHub PR)

파싱(`parse_diff_text`)은 git 호출과 분리된 순수 함수입니다. PR diff를 지원하려면:

```python
# diff/collector.py에 추가
def collect_pr_diff(diff_text: str, base_ref: str) -> DiffScope:
    """GitHub API 등에서 받은 unified diff 텍스트를 그대로 파싱."""
    return DiffScope(base_ref=base_ref, files=parse_diff_text(diff_text))
```

`gh pr diff <num>` 출력이나 `Accept: application/vnd.github.diff` API 응답을 그대로 넣으면 됩니다.

## 새 분석기 추가 (clang-tidy 외)

Stage 2의 계약은 `run(scope: DiffScope) -> list[RawDiagnostic]` 하나입니다.
같은 시그니처의 러너를 만들어 `cli.run_pipeline`에서 결과를 이어붙이면 됩니다 (예: cppcheck, libclang AST 워커).
`RawDiagnostic.check`에 분석기 접두사를 붙여 출처를 구분하세요 (`cppcheck-nullPointer` 등).

## 리포트 포맷 추가

Stage 4의 계약은 `render_*(result: ReviewResult) -> str`입니다.
`report/` 아래에 `sarif.py`(GitHub code scanning용), `json.py` 등을 추가하고 CLI에 `--format` 옵션을 붙이면 됩니다.

## LLM 관련 조정 포인트

| 조정 | 위치 | 비고 |
|---|---|---|
| 모델 변경 | CLI `--model` 또는 `config.DEFAULT_MODEL` | 기본 `claude-opus-4-8` |
| 프롬프트 | `llm/postprocess.py` `_SYSTEM_PROMPT` | 실패 시나리오를 구체적으로 쓰게 하는 문구가 핵심 |
| 출력 스키마 | `_Verdict`, `_ExtraFinding`, `_LlmReview` | Pydantic 모델 수정만으로 스키마 강제 유지 |
| 대형 diff 청킹 | `process()` 호출 전 `DiffScope` 분할 | 파일 단위 분할 → 호출 병렬화 순서로 |

## 상수 (config.py)

| 상수 | 기본값 | 의미 |
|---|---|---|
| `LINE_FILTER_MARGIN` | 15 | 변경 범위 확장폭 — 동시성 문맥 확보용. 노이즈가 늘면 줄일 것 |
| `SHALLOW_MODE_STD` | c++17 | 얕은 모드 기본 표준. 대상 레포에 맞게 CLI 옵션화 가능 |
| `COMPILE_DB_CANDIDATES` | `.`, `build`, `out`, ... | compile_commands.json 탐색 경로 |
| `CPP_EXTENSIONS` | .cpp/.h 등 | diff에서 C++로 취급할 확장자 |
