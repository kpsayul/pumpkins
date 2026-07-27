# 아키텍처

> 이 문서는 **현재 구현된** 파이프라인의 내부 구조와 설계 결정을 설명합니다. 실행 방법은 [README](../README.md) 참고.
>
> 제품의 지향점(프로젝트별 코드 규칙 위반 + 기능적 문제를 사람 리뷰어처럼 짚어주기)은 [README](../README.md)에 있고,
> 아래는 그중 **기능적 문제(동시성)** 축을 먼저 구현한 첫 조각입니다. 컨벤션 규칙 축은 같은 4단계 뼈대 위에
> 프로파일과 LLM 프롬프트를 얹는 형태로 확장할 예정입니다.

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

각 단계는 [models.py](../src/pumpkins/models.py)에 정의된 Pydantic 모델만으로 통신합니다.
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
- **탈락한 파일은 버리지 않고 `DiffScope.skipped_files`에 기록** — Stage 4가 "안 봤다"고 말할 수 있어야 함. 실측 계기: 어떤 PR에서 변경 8개 중 6개(비C++ 동반 파일)가 여기서 조용히 사라졌는데 리포트는 `✅ No findings`였음.
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
- 스킵한 헤더는 `runner.skipped_headers`로 올려보내 리포트에 명시하고, WARNING 로그를 남김. **헤더 온리 프로젝트에서는 이 규칙이 구현 전체를 날린다** — 실측: 헤더가 소스보다 네 배 이상 많고 구현이 전부 헤더에 인라인된 리포에서는 이 축이 영구히 0건이고, fmt PR도 로직 변경분인 `include/fmt/format.h`가 여기서 빠졌음. compile DB 생성이 유일한 해법이므로 리포트가 그걸 안내함.

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
| 프로바이더 | `LLM_PROVIDER` 환경변수로 anthropic/openai 선택 (`llm/provider.py` 어댑터) |
| 모델 | anthropic: `claude-opus-4-8` / openai: `gpt-4o` (기본, `--model`로 변경 가능) |
| API | 프로바이더별 구조화 출력 호출(anthropic `messages.parse` / openai `beta.chat.completions.parse`) + Pydantic 스키마 → 구조화 출력 강제 |

LLM의 역할은 두 갈래:

1. **Triage** — 번호 붙인 진단 각각에 `keep/drop`, 심각도, 제목, 구체적 실패 시나리오 설명, 수정 제안을 판정. 얕은 모드일 때는 프롬프트에 "false positive 가능성 높음"을 명시해 보수적으로 판단하게 함.
2. **독립 탐지 (`extra_findings`)** — clang-tidy가 구조적으로 못 잡는 패턴을 diff에서 직접 탐지:
   - lock 획득 순서 역전 (경로별 불일치)
   - guard mutex 없이 읽고/쓰는 공유 멤버
   - 동기화 대용으로 쓰인 volatile
   - predicate 없는 condition variable wait
   - happens-before 없는 스레드 간 데이터 공개

   → 요청한 핵심 버그 클래스의 실질 커버리지는 이쪽입니다. clang-tidy는 정밀 필터, LLM은 넓은 그물.

인증: 각 SDK가 자기 키(`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`)를 환경변수에서 직접 읽음. 코드·커밋되는 설정 파일에 키 없음. CLI 진입 시 cwd의 `.env`를 자동 로드하되 실제 환경변수가 우선(`override=False`) — 설계는 [llm-provider-and-keys-design.md](llm-provider-and-keys-design.md).

방어 로직: LLM이 반환한 verdict index가 범위 밖이면 경고 후 무시. 파싱 결과가 None이면 명시적 에러.

### Stage 3.5 — 컨벤션 대조 (`conventions/checker.py`)

| 항목 | 내용 |
|---|---|
| 입력 | `DiffScope` + `<repo>/conventions/rules/`의 **활성** 규칙 (`facet`/`value`/`scope`) |
| 출력 | 질문형 `Finding[]` (`source: convention`, severity `low` 고정 — 네이밍이 버그를 이기지 않게) |
| 실행 조건 | `conventions/`(또는 레거시 `conventions.yml`) 존재 시 자동 (`--conventions PATH` / `--no-conventions`) — **LLM·API 키 불필요, 결정적** |

- **`candidates/`의 규칙은 적용되지 않습니다.** 승인이 실제 게이트여야 하므로 `load_active_rules`가 `rules/`만 읽고, 대기 중 후보 수는 리포트에 표시합니다 — 안 그러면 "학습했는데 지적이 없네"가 통과로 읽힙니다.

- diff의 **추가 라인에서 선언된 식별자만** 검사. 멤버 변수 매칭은 hunk에 클래스 스캐폴딩(`private:` 등)이 보일 때만 — 없으면 지역 변수로 간주하고 건너뜀 (오탐 억제).
- **규칙마다 `scope`로 적용 파일을 제한** (`conventions/scope.py`) — 파일별로 in-scope 규칙만 대조. 레거시·생성 코드 트리가 자기 관행을 유지할 수 있는 장치.
- **casing은 관용 비교** — 단어 경계가 없는 소문자 한 단어(`flush`)는 lowerCamel과 lower_snake를 동시에 만족하므로 casing 규칙을 위반하지 않음 (`extractor.casing_matches`).
- 지적은 질문형 + 근거 수치 + 적용 범위 + (prefix/suffix 규칙이면) rename 제안. `(file, rule, name)` 단위로 중복 제거.
- `facet: other` 규칙은 기계 대조 불가 — 파일에는 남고, LLM 문맥 보조(설계 문서 방안 B)가 후속으로 담당.

### Stage 4 — 리포트 (`report/markdown.py`)

- 헤더 메타데이터: 생성 시각, diff base, 프로파일, **분석 모드(얕은 모드 경고 포함)**, **LLM 프로바이더/모델**, 활성 규칙 수와 미승인 후보 수, **커버리지(분석한 C++ 파일 / 변경된 C++ 파일, 안 읽은 비C++ 파일 수)**, 진단 수 흐름 (`raw → dropped → findings`).
- **커버리지 공백이 있으면 `✅`를 절대 쓰지 않는다.** 못 본 파일 목록을 이유와 함께 먼저 출력하고, finding이 0건이면 "통과로 읽지 마세요"로 마무리. 침묵과 통과를 구분하지 못하는 리포트가 리뷰 도구의 가장 위험한 실패 모드라서, `ReviewResult.has_coverage_gap`이 이 분기를 강제함.
- Finding은 심각도순 정렬(critical→info), 심각도별 이모지 요약.
- stdout이 기본 출력이라 파이프 가능 — 로그는 전부 stderr (`config.setup_logging`).

### 실행 산출물 (`report/dump.py`, `--out-dir`)

`out/{report.md, run.json, diff.patch, diagnostics.json, findings.json, llm/{request,response}}`.

`run_pipeline`이 `(ReviewResult, RunContext)`를 반환하는 이유가 이것입니다 — 프롬프트·원본 diff는
**디버깅 재료지 리뷰 출력이 아니므로** `ReviewResult`(리포트의 계약)에 섞지 않고 별도 dataclass로 나릅니다.

- `run.json`은 실행 출처입니다: pumpkins 버전, repo 커밋, 프로바이더/모델, **clang-tidy 버전**,
  `rules_fingerprint`, 커버리지. 모델을 바꿨을 때 차이가 모델·규칙·도구 중 무엇 때문인지 가리려면
  이 세 축이 결과와 함께 고정돼 있어야 합니다.
- `rules_fingerprint`는 활성 규칙 파일들의 sha256(앞 16자)입니다. git SHA는 커밋 전에 없지만
  내용 해시는 항상 있고, "두 실행의 규칙이 같았나"에 바로 답합니다. **미승인 후보는 제외** —
  적용되지 않으므로 실행의 정체성에 들어가지 않습니다.
- 이전 실행의 산출물은 쓰기 전에 지웁니다. 디버깅 중에는 없는 파일보다 낡은 파일이 더 위험합니다
  (예: `--no-llm` 실행에 지난번 프롬프트가 남아 있으면 잘못된 귀인을 합니다).
- `llm/request.txt`가 실질 가치가 가장 큽니다 — "왜 이런 지적을 했지"의 답이 대개 프롬프트에 있습니다.

## Learn 파이프라인 (`conventions/`) — `pumpkins learn`

리뷰 파이프라인과 별개로 도는 2단계 흐름. 설계 근거는 [convention-detection-design.md](convention-detection-design.md).

```
repo 스캔 ──▶ CategoryStats[] ──▶ LearnResult ──▶ Reconciliation ──▶ conventions/
        conventions/extractor   conventions/learner   conventions/store   (대상 리포에 커밋)
```

`learn`은 **1회성/주기적**, 리뷰는 **매 PR**. 리뷰는 규칙 파일을 읽기만 하므로 규칙 생성 비용을 내지 않습니다 (설계 문서 §2 방안 A).

| 단계 | 내용 |
|---|---|
| L1 `extractor.py` | 정규식 기반 C++ 식별자 추출(멤버/**상수**/함수/클래스) → 접두사·접미사·casing 분포 통계. **LLM엔 이 통계+샘플만 전달** — 파일 원문은 절대 안 보냄 (토큰 비용 절감). 파서가 아닌 휴리스틱 — tree-sitter가 업그레이드 경로 |
| L2 `learner.py` | LLM(learn 기본 모델 — anthropic: Sonnet / openai: gpt-4o-mini)이 규칙 후보 판정 → **코드 측 임계선 게이트**(`MIN_RULE_OCCURRENCES`/`MIN_RULE_CONSISTENCY`)가 LLM 판단과 무관하게 미달 규칙을 강등. 이 모듈은 무엇을 *제안*할지만 정하고, 운명은 결정하지 않음 |
| L3 `store.py` | 기존 결정과 병합(`reconcile`) → `conventions/`에 규칙당 파일 하나로 기록. 후보는 `candidates/`, 근거·통계는 `config.yml` |

### 규칙 저장소 (`conventions/store.py`)

```
conventions/{config.yml, rules/, candidates/, archive/}
```

두 가지 결정이 이 모듈의 형태를 정합니다.

**상태는 디렉터리다.** 규칙이 놓인 폴더가 그 규칙의 상태이므로 파일 안 `status:` 필드와 실제가 어긋날 수 없고, 상태 전이가 `git mv` 한 번이라 "누가 언제 승인했는가"를 git이 자동 기록합니다 — 승인자 필드를 손으로 관리하지 않습니다.

**이력은 git에 맡긴다.** 규칙 하나 = 파일 하나이므로 `git log --follow conventions/rules/<id>.yml`이 이력입니다. 파일 안 `history:` 배열은 작성자 신원도 서명도 없는 git 재구현이라 두지 않았고, 파일에는 git이 줄 수 없는 것 — 결정의 **이유** — 만 남깁니다.

규칙당 파일 하나인 이유: learn이 기계적으로 파일을 씁니다. 카테고리별로 묶으면 규칙 하나가 바뀔 때 무관한 규칙까지 재작성돼 git 이력이 더러워지고 동시 추가 시 충돌합니다.

### 재실행 병합 (`reconcile`)

**고친 결함**: 이전 learn은 파일을 통째로 덮어써서, 사용자가 틀린 규칙을 지우고 재실행하면 되살아났습니다 — **검수하면 검수한 만큼 손해**를 보는 구조였고, §3-(1)의 "사용자가 승인/수정/삭제 가능"을 재실행이 무효화했습니다.

`reconcile`은 파일시스템을 건드리지 않는 순수 함수(dict in / dict out)라 아래 다섯 경우를 단위 테스트로 고정할 수 있습니다.

| 상황 | 동작 |
|---|---|
| id 없음 | 새 후보 → `candidates/` |
| 활성 + facet/value 동일 | 근거 수치만 갱신, 결정과 `reason` 보존 (통계 변동은 새 결정이 아님) |
| `archive/`에 있음 | 재제안 생략 — 기각은 "없음"이 아니라 결정이다 (`--reconsider`로 해제) |
| 같은 category/facet에 다른 값 | 대체 제안. 활성은 그대로 두고, 후보 파일에 무엇을 대체하는지 기록 |
| 활성인데 새 스캔이 뒷받침 못함 | 은퇴 후보로 **보고만** 함 (자동 삭제 금지) |

값이 바뀌면 id도 바뀌므로(`member-prefix-m_` → `member-prefix-m`) 동일성은 id로, 충돌 탐지는 `(category, facet)`으로 판단합니다.

승인 경로는 **비대화형이 기본**입니다 — 후보를 파일로 쓰고 사람이 PR에서 옮깁니다. CI에서 돌고, 결정이 리뷰 가능한 커밋으로 남기 때문입니다.

### 하위 호환

`load_active_rules`가 디렉터리와 레거시 단일 `conventions.yml`을 모두 읽습니다. 새 실행은 디렉터리만 쓰고, 옛 파일은 계속 읽힙니다. 검증 픽스처를 일부러 레거시 포맷으로 남겨 둬서, 하네스 [1]~[3]단계가 그 경로를 계속 검증하고 [4]단계가 두 포맷의 등가성을 확인합니다.

### L1의 측정 규칙 — 실제 리포에서 틀렸던 것들

통계가 조금 틀리면 규칙이 정반대로 뒤집힌다. 아래는 실제 리포 두 곳(비공개 리포·fmt)에서 발견해 고친 항목들이고, 각 결정의 근거는 코드 주석에도 남겨 뒀다.

| 규칙 | 이유 |
|---|---|
| **casing 분모를 따로 둔다** (`casing_informative`) | 소문자 한 단어(`dump`, `value`)는 lowerCamel/lower_snake를 동시에 만족해 casing 정보가 없음. 별도 버킷으로 세면 하나의 관행이 쪼개짐 — fmt 멤버는 전부 snake_case인데 single_lower 66% / lower_snake 33%로 갈려 85% 게이트에서 탈락했고, 분모를 고친 뒤 100%로 복구됨 |
| **밑줄 없는 헝가리안 접두사 인식** (`m[A-Z]`/`k`/`s`/`g`) | 어떤 리포는 `mFoo` 스타일. `m_`만 알던 시절엔 멤버 91%가 "접두사 없음"으로 집계돼 *"멤버는 접두사를 쓰지 않는다"* 는 정반대 규칙이 채택 조건을 통과했고, 그 규칙은 그 리포의 `kFoo` 상수 수백 개를 모두 위반으로 지적하는 오탐 생성기였음. 지금은 `m` 98% |
| **상수를 멤버와 분리** (`constant` 카테고리) | `static constexpr`/`static const`만 상수로 분류(`const T&` 멤버는 제외). 섞으면 접두사 분포가 오염됨 |
| **전처리기 줄·템플릿 파라미터·ALL_CAPS 배제** | `template <class Char>`의 `Char`가 클래스로, 매크로가 함수로 집계됨. fmt 타입의 41%가 UpperCamel로 보인 원인 |
| **테스트 디렉터리 기본 제외** (`LEARN_TEST_DIRS`, `--include-tests`로 해제) | 테스트는 관행이 느슨하고, 벤더링된 테스트 프레임워크가 여기 숨음. fmt의 `test/gtest/`(번들 googletest)가 UpperCamel 함수/클래스 2845건 중 2837건을 공급해 snake_case 리포를 63% UpperCamel로 보이게 했음 |
| **디렉터리별 식별자 수를 로그로 출력** | 위 gtest 오염을 찾아낸 방법이 이거였음. 정체불명 벤더 트리는 항상 과대 기여자로 드러남 |

### 스캔 범위와 규칙 scope (`conventions/scope.py`)

`learn`이 **읽는 범위**와 규칙이 **판정하는 범위**가 같은 어휘(`paths`/`exclude_paths`/`extensions`)를 쓴다.
`--include`로 좁혀 학습하면 나온 규칙에 그 범위가 그대로 `scope`로 박힌다 — 측정한 코드 밖에서는 규칙을 신뢰할 수 없으므로, 이 전파는 LLM이 아니라 코드가 한다(임계선 게이트와 같은 원칙).

패턴은 리포 상대 POSIX 경로에 **대소문자 구분**으로 매칭(`fnmatchcase`) — 같은 규칙 저장소가 어느 OS에서도 같은 결과를 내야 하므로. `*`는 디렉터리 경계를 넘고, 디렉터리를 가리키는 패턴은 그 아래 전체를 덮는다.

실패 처리: C++ 식별자 0개면 에러(exit 1), API 키 없으면 에러 + `--no-llm`(통계 덤프) 안내.

## 체크 프로파일 (`analysis/checks.py`)

`CHECK_PROFILES` dict에 버그 클래스별 clang-tidy 체크 목록. 현재 `concurrency`만 존재.
의도적으로 **고정밀·저노이즈** 위주로 좁게 유지 — 넓은 탐지는 LLM extra_findings가 담당.
확장 방법은 [extending.md](extending.md) 참고.

## 실패 처리 원칙

| 상황 | 동작 |
|---|---|
| diff에 C++ 변경 없음 | 경고 후 빈 리포트 (exit 0) |
| clang-tidy 미설치 | 즉시 에러 (exit 1) |
| 선택된 프로바이더의 API 키 없음 (`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`) | 경고 후 `--no-llm` 동작으로 폴백 |
| `LLM_PROVIDER` 값이 anthropic/openai 밖 | 즉시 에러 (exit 2) |
| LLM 파싱 실패 | 에러 (exit 1) — 조용히 미검증 결과를 내지 않음 |
| 얕은 모드 컴파일 에러 | 버리고 계속 (예상된 노이즈) |

## 의도적으로 안 한 것 (프로토타입 범위 제한)

- 웹서버 / GitHub App / CI 연동 — 검증 결과가 나온 뒤 판단
- libclang AST 분석 — subprocess clang-tidy로 충분한지 먼저 확인
- 대형 diff 청킹, LLM 호출 병렬화
- 결과 캐싱, 증분 분석
