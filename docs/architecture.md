# 아키텍처

> 이 문서는 파이프라인의 내부 구조와 설계 결정을 설명합니다. 실행 방법은 [README](../README.md) 참고.

## 전체 흐름

```
┌─────────────┐    ┌──────────────────┐    ┌───────────────────┐    ┌──────────────┐
│ git diff     │───▶│ Stage 1          │───▶│ Stage 2           │───▶│ Stage 3      │
│ (local/PR)   │    │ diff/collector   │    │ analysis/         │    │ llm/         │
└─────────────┘    │                  │    │ clang_tidy        │    │ postprocess  │
                   │ DiffScope        │    │ RawDiagnostic[]   │    │ Finding[]    │
                   └──────────────────┘    └───────────────────┘    └──────┬───────┘
                                                                          ▼
                                                                   ┌──────────────┐
                                                                   │ Stage 4      │
                                                                   │ report/      │
                                                                   │ markdown     │
                                                                   │ report.md    │
                                                                   └──────────────┘
```

각 단계는 [models.py](../src/cpp_review_bot/models.py)에 정의된 Pydantic 모델만으로 통신합니다.
단계 간 의존성이 데이터 모델뿐이므로, 어떤 단계든 독립적으로 교체·테스트할 수 있습니다.

## 단계별 상세

### Stage 1 — diff 수집·파싱 (`diff/collector.py`)

| 항목 | 내용 |
|---|---|
| 입력 | 레포 경로 + base ref (선택) |
| 출력 | `DiffScope` — 파일별 추가 라인 범위 + 원본 patch 텍스트 |
| git 호출 | base 지정 시 `git diff <base>...HEAD` (PR과 동일한 3-dot), 미지정 시 `git diff HEAD` (워킹트리) |

설계 포인트:

- **git 호출(`collect_diff`)과 파싱(`parse_diff_text`)을 분리** — 파서는 순수 함수라 단위 테스트가 쉽고, 나중에 GitHub API로 받은 PR diff 텍스트를 그대로 먹일 수 있음.
- C++ 확장자(`config.CPP_EXTENSIONS`)만 통과. 삭제된 파일은 제외 (새 코드가 없으므로).
- 추가 라인들은 **3줄 이하 간격이면 하나의 범위로 병합** (`_merge_into_ranges`) — line-filter JSON이 불필요하게 길어지는 것 방지.
- `patch_text`는 Stage 3에서 LLM 문맥으로 그대로 사용.

### Stage 2 — 정적 분석 (`analysis/clang_tidy.py`)

| 항목 | 내용 |
|---|---|
| 입력 | `DiffScope` |
| 출력 | `RawDiagnostic[]` (변경 범위 내 진단만) |
| 실행 | clang-tidy를 파일당 1회 subprocess로 호출 |

**절대 빌드하지 않는다**는 제약을 두 가지 모드로 처리:

| 모드 | 조건 | 동작 | 신뢰도 |
|---|---|---|---|
| compile-DB | `compile_commands.json` 발견 (`config.COMPILE_DB_CANDIDATES` 위치 탐색) | `-p <dir>`로 실제 플래그 사용 | 높음 |
| 얕은(shallow) | compile DB 없음 | `clang-tidy file.cpp -- -std=c++17 -I<repo> -I<repo>/include -I<repo>/src` | 낮음 — 리포트에 명시 |

얕은 모드의 처리 규칙:

- 헤더 파일은 단독 컴파일이 불가능하므로 **TU(.cpp/.cc/...)만 분석** — 헤더 변경분은 Stage 3의 LLM이 diff 문맥으로 커버.
- 헤더를 못 찾아 나오는 `clang-diagnostic-*` error는 리뷰 대상이 아니므로 파싱 단계에서 버림 (개수만 debug 로그).

변경 라인 제한:

- clang-tidy `--line-filter`(JSON)에 각 범위를 **±15줄(`LINE_FILTER_MARGIN`) 확장**해 전달.
  동시성 버그는 lock을 잡는 라인과 접근하는 라인이 떨어져 있는 경우가 많아 여유분이 필요.
- line-filter는 *분석 범위*가 아니라 *리포트 범위*를 제한함 — clang-tidy는 파일 전체를 분석하되 해당 라인의 진단만 출력.

출력 파싱:

- 현재는 텍스트 출력 정규식 파싱 (`_DIAG_RE`). `--export-fixes` YAML 전환이 다음 단계 (note 진단·fix hint까지 구조화 가능).
- clang-tidy는 진단이 있으면 non-zero exit — 종료 코드는 성공/실패 판단에 사용하지 않음.

### Stage 3 — LLM 후처리 (`llm/postprocess.py`)

| 항목 | 내용 |
|---|---|
| 입력 | `DiffScope` + `RawDiagnostic[]` + shallow 여부 |
| 출력 | `Finding[]` + 노이즈로 버린 개수 |
| 모델 | `claude-opus-4-8` (기본, `--model`로 변경 가능) |
| API | `client.messages.parse()` + Pydantic 스키마 → 구조화 출력 강제 |

LLM의 역할은 두 갈래:

1. **Triage** — 번호 붙인 진단 각각에 `keep/drop`, 심각도, 제목, 구체적 실패 시나리오 설명, 수정 제안을 판정. 얕은 모드일 때는 프롬프트에 "false positive 가능성 높음"을 명시해 보수적으로 판단하게 함.
2. **독립 탐지 (`extra_findings`)** — clang-tidy가 구조적으로 못 잡는 패턴을 diff에서 직접 탐지:
   - lock 획득 순서 역전 (경로별 불일치)
   - guard mutex 없이 읽고/쓰는 공유 멤버
   - 동기화 대용으로 쓰인 volatile
   - predicate 없는 condition variable wait
   - happens-before 없는 스레드 간 데이터 공개

   → 요청한 핵심 버그 클래스의 실질 커버리지는 이쪽입니다. clang-tidy는 정밀 필터, LLM은 넓은 그물.

인증: `anthropic.Anthropic()`가 `ANTHROPIC_API_KEY` 환경변수를 직접 읽음. 코드·설정 파일에 키 없음.

방어 로직: LLM이 반환한 verdict index가 범위 밖이면 경고 후 무시. `parsed_output`이 None이면 명시적 에러.

### Stage 4 — 리포트 (`report/markdown.py`)

- 헤더 메타데이터: 생성 시각, diff base, 프로파일, **분석 모드(얕은 모드 경고 포함)**, LLM 사용 여부, 진단 수 흐름 (`raw → dropped → findings`).
- Finding은 심각도순 정렬(critical→info), 심각도별 이모지 요약.
- stdout이 기본 출력이라 파이프 가능 — 로그는 전부 stderr (`config.setup_logging`).

## 체크 프로파일 (`analysis/checks.py`)

`CHECK_PROFILES` dict에 버그 클래스별 clang-tidy 체크 목록. 현재 `concurrency`만 존재.
의도적으로 **고정밀·저노이즈** 위주로 좁게 유지 — 넓은 탐지는 LLM extra_findings가 담당.
확장 방법은 [extending.md](extending.md) 참고.

## 실패 처리 원칙

| 상황 | 동작 |
|---|---|
| diff에 C++ 변경 없음 | 경고 후 빈 리포트 (exit 0) |
| clang-tidy 미설치 | 즉시 에러 (exit 1) |
| `ANTHROPIC_API_KEY` 없음 | 경고 후 `--no-llm` 동작으로 폴백 |
| LLM 파싱 실패 | 에러 (exit 1) — 조용히 미검증 결과를 내지 않음 |
| 얕은 모드 컴파일 에러 | 버리고 계속 (예상된 노이즈) |

## 의도적으로 안 한 것 (프로토타입 범위 제한)

- 웹서버 / GitHub App / CI 연동 — 검증 결과가 나온 뒤 판단
- libclang AST 분석 — subprocess clang-tidy로 충분한지 먼저 확인
- 대형 diff 청킹, LLM 호출 병렬화
- 결과 캐싱, 증분 분석
