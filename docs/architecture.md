# 아키텍처

> pumpkins의 **현재 구조**를 추상적으로 설명합니다 — 무엇이 어떤 책임을 지고, 무엇으로 연결되는가.
> *"왜 이렇게 됐나"*(측정·시행착오·판단 근거)는 [design-history.md](design-history.md)에 있습니다.
> 실행 방법은 [README](../README.md), 컨벤션 축의 설계 배경은 [convention-detection-design.md](convention-detection-design.md).

## 한눈에

pumpkins는 C++ 코드 리뷰 도우미다. **규칙을 만드는 `learn`** 과 **규칙을 쓰는 `review`**, 두 국면으로 나뉜다.

```
① learn  (규칙 만들기 · 가끔)                      pumpkins/  (규칙 저장소)
    리포 코드 ──▶ [규칙 추출] ──▶ candidates/ ──(사람 승인)──▶ rules/
                                                                  │
                                                                  ▼
② review (규칙 쓰기 · 매 PR)                                  report.md
    git diff  ──▶ [검사] ──▶ 리포트  ◀────────── rules/ 를 읽어 적용
```

- **`learn`** — 리포에서 이 프로젝트의 컨벤션을 뽑아 `pumpkins/` 규칙 저장소로 만든다.
- **`review`** — git diff를 검사해 마크다운 리포트를 낸다. 규칙 저장소를 **읽기만** 한다.

두 국면은 **규칙 저장소(파일)로만 연결**된다. 그래서 서로 독립적이고, review는 규칙 생성 비용을 내지 않는다.

## 모듈 지도

| 모듈 | 역할 | 국면 |
|---|---|---|
| `diff/collector.py` | git diff 수집·파싱 → 변경 라인 범위 + 문맥 | review |
| `analysis/clang_tidy.py` | clang-tidy를 별도 프로세스로 실행 → 진단 | review |
| `analysis/checks.py` | 프로파일별 clang-tidy 체크 목록 | review |
| `analysis/cxx_standard.py` | 프로젝트가 선언한 최소 C++ 표준 감지 | review |
| `profiles.py` | 리뷰 프로파일 정의(체크 + LLM 지시) — `concurrency`, `portability` | review |
| `llm/provider.py` | 프로바이더 중립 구조화 출력 클라이언트(anthropic/openai) | 공통 |
| `llm/postprocess.py` | LLM triage — 노이즈 필터·심각도·설명·추가 탐지·규칙 판정 | review |
| `report/markdown.py` | 마크다운 리포트 렌더 | review |
| `report/dump.py` | `--out-dir` 실행 산출물(진단·프롬프트·출처) | review |
| `conventions/extractor.py` | 이름 통계 — AST로 스캔하고 facet 어휘로 분해 (learn 통계 경로) | learn |
| `conventions/learner.py` | 통계 → LLM 규칙 판정 + 임계선 게이트 | learn |
| `conventions/survey.py` | LLM 없이 리포 구조 요약 — 디렉터리·include 방향·클래스/멤버 (1차 훑기 입력) | learn |
| `conventions/proposer.py` | 틀 없이 코드에서 규칙 추측 — 싼 모델 훑기 → 강한 모델 정독 (learn 추론 경로) | learn |
| `conventions/verifier.py` | 추측을 리포에 대조·채점 | learn |
| `conventions/checker.py` | 규칙을 diff와 결정적 대조 (명명=선언 매칭, 구조=계층/AST) | review |
| `conventions/scope.py` | 규칙·스캔의 적용 범위(경로/확장자) | 공통 |
| `conventions/store.py` | 규칙 저장소 상태·병합·기록 | learn |
| `languages/cpp/ast.py` | C++ AST(tree-sitter) — 식별자·함수·멤버·클래스 추출 (네이티브 dep, import-가드로 격리) | 공통 |
| `languages/cpp/parser.py` | 정규식 C++ 파서 — 리뷰 hunk 검사 + AST 폴백 | 공통 |
| `languages/cpp/naming.py` | facet 어휘 — 이름을 prefix/suffix/casing로 분해 | 공통 |
| `models.py` | 단계 간 데이터 계약 | 공통 |
| `config.py` | 전역 설정·모델 선택·상수 | 공통 |

## 데이터 계약 (`models.py`)

파이프라인 단계는 [models.py](../src/pumpkins/models.py)의 Pydantic 모델로만 통신한다. 단계 간 의존성이
데이터 모델뿐이라 어떤 단계든 독립적으로 교체·테스트할 수 있다.

- `DiffScope` — 변경된 파일과 그 추가 라인 범위 + 문맥.
- `RawDiagnostic` — clang-tidy 진단 하나 (triage 전).
- `Finding` — 리포트에 실릴 지적 하나. `Evidence`를 **필수**로 갖는다.
- `Evidence` — 지적의 출처: 어떤 **detector**(clang-tidy/convention/llm)가, 어떤 **규칙**으로 판단했고,
  같은 입력에 **재현되는가(`reproducible`)**. 재현성은 detector에서 자동으로 정해진다.
- `ReviewResult` — 리포트 렌더러에 넘기는 최종 출력.

## learn — 규칙을 만든다

두 경로가 같은 저장소(`candidates/`)로 모인다. 하나는 **틀에 맞춰 측정**하고, 하나는 **틀 없이 추측 후 검증**한다.

### [A] 통계 경로 (기본)

```
코드 ──▶ 이름 통계 ──▶ LLM 판정(2단계) ──▶ 게이트 ──▶ 후보
      extractor       learner                        (facet 규칙)
```

- **extractor** — tree-sitter AST(`cpp_ast`)로 식별자를 뽑고, facet 어휘(`naming`)로 분해해 카테고리별
  (멤버/상수/함수/클래스) 이름 통계(prefix / suffix / casing 분포)를 낸다. LLM에는 파일 원문이 아니라 이
  통계만 준다.
- **learner** — 통계를 LLM에 주고 규칙 후보를 판정한다. **2단계**로 나뉜다: 싼 모델이 규칙을 분류하고,
  한 카테고리가 두 무리로 갈린 "숨은 쪼개짐"의 **추론**만 강한 모델로 자동 승급한다.
- **게이트** — 같은 패턴 **20회 이상 + 일관성 85% 이상**만 규칙으로 채택한다. LLM 판단과 무관하게 **코드가 강제**한다.
- 결과는 기계로 검사 가능한 **facet 규칙**(prefix / suffix / casing + 값)이다.

### [B] 추론 경로 (`--infer`, 선택)

```
전체 코드 ──▶ 구조 요약 ──▶ 어디를 볼지 ──▶ 지목된 파일만 ──▶ 규칙 추측 ──▶ 대조·채점 ──▶ 후보
              survey        (싼 모델)         정독 (강한 모델)    proposer      verifier
              LLM 없음       triage                                            (검증됨/기각/미검증)
```

- **survey** — LLM 없이 리포의 *생김새*만 뽑는다: 디렉터리, 디렉터리 간 include 방향,
  클래스와 그 상속·멤버 타입. 파싱이라 공짜고 결정적이며, 한 프롬프트에 리포 전체가 들어간다.
- **1차 훑기 (싼 모델)** — 그 요약만 읽고 *"어디에 이 리포만의 관행이 있을 법한가, 어느 파일을 열어보면
  보이는가"* 만 답한다. 짚이는 곳이 없으면 강한 모델은 **아예 돌지 않는다**.
- **정독 (강한 모델)** — 지목된 파일 몇 개의 **전문** + 구조 요약을 함께 받아 규칙을 추측하고,
  각 추측에 실행 가능한 구조화 check를 붙인다.
  요약을 같이 주는 이유: **총합에만 존재하는 관행이 있다.** "src가 include/를 81번 참조하고 역방향은 0회"는
  97개 파일에 대한 사실이라 그중 7개를 읽어서는 절대 보이지 않는다 —
  실측으로, 요약을 주기 전엔 계층 규칙이 하나도 안 나왔다.
- **verifier** — 그 check를 **리포 전체에 대조해 실측 coverage**를 내고, 통계 경로와 **같은 게이트**로 판정한다.
  - **검증됨** — coverage가 게이트를 넘음. 명명 규칙이면 facet 규칙으로 승격된다.
  - **기각** — coverage 미달. 버린다 (틀린 추측을 거르는 필터).
    "리포가 반박함"과 "표본이 모자라 판단 불가"는 사람이 할 행동이 달라 **다르게 적는다**.
  - **미검증** — 실행 가능한 check가 없음. `facet: other` 가설로 남는다.

**제안은 열되, 채택은 리포 코드가 잠근다** — 이것이 두 경로의 공통 규율이다(게이트, 그리고 검증).

**모델을 나누는 기준은 파이프라인 단계가 아니라 일의 성질이다.** "여기 뭔가 있나"는 싼 모델이 잘하고,
"그래서 규칙이 정확히 무엇인가"는 강한 모델이 필요하다 — learn의 쪼개짐 승급과 같은 원리다.

#### 구조 check 어휘

| check | 무엇을 재나 | 분모 | 필요한 것 |
|---|---|---|---|
| `naming` | 접두사·접미사·casing | 해당 카테고리의 식별자 | 정규식 |
| `header_directive` | 헤더 첫 줄 (`#pragma once` 등) | 헤더 파일 | 정규식 |
| `include_direction` | **계층 방향** — 이 계층이 저 계층을 참조하지 않는다 | **그 계층의 파일** | 정규식 |
| `return_type` | `create*`는 `unique_ptr`를 반환한다 | 이름이 맞는 함수 | tree-sitter |
| `member_ownership` | **소유권** — 포인터 멤버를 스마트 포인터로 잡는다 | **포인터를 쥔 멤버** | tree-sitter |
| `base_class` | **상속** — `*Exception`은 X를 상속한다 | 이름이 맞는 클래스 | tree-sitter |

- **분모는 규칙이 말하는 모집단으로 잡는다.** 값으로 가진 멤버까지 세면 소유권 coverage는
  "전체 필드 중 스마트 포인터 비율"이 되고, 계층 규칙을 레포 전체로 재면 큰 무관한 코드가
  어떤 방향 규칙이든 공짜로 통과시킨다. 이 프로젝트가 이미 두 번 치른 실수다.
- **계층 검사는 일부러 tree-sitter에 기대지 않는다.** `#include`는 한 줄로 읽히고, 셋 중 가장
  파급이 큰 규칙을 네이티브 빌드 실패로 잃을 이유가 없다.

### 규칙 저장소 (`pumpkins/`)

```
pumpkins/
├── settings.yml     사람이 씀 — 확장자 판정 등 (learn이 건드리지 않음)
├── learn-report.yml learn이 씀 — 지난 학습의 근거·통계
├── rules/           활성 — 리뷰가 적용하는 것은 여기뿐
├── candidates/      판단 대기 — 리뷰에 영향 없음
└── archive/         기각·은퇴 — learn이 다시 제안하지 않음
```

- **상태 = 디렉터리.** 규칙이 놓인 폴더가 상태다. 상태 전이는 `git mv` 한 번이고, git이 승인자·시각을 기록한다.
- **규칙 하나 = 파일 하나.** 이력은 git(`git log --follow`)이 관리하고, 파일에는 git이 줄 수 없는 결정의 *이유*만 둔다.
- **채택 게이트는 사람.** `candidates/`는 승인 전까지 리뷰에 영향이 없다.
- **`scope`** — 규칙마다 적용 경로/확장자 범위. 레거시·생성 코드 트리가 자기 관행을 유지하게 한다.

## review — 규칙을 쓴다

git diff를 **세 detector**가 각자 훑고, 결과가 **하나의 리포트로 병합**된다.

```
git diff ─┬─▶ clang-tidy ──────────────┐   버그(동시성·이식성)
          ├─▶ LLM triage ──────────────┤   진단 걸러내기 + diff 직접 탐지 + 규칙 판정
          └─▶ convention checker ──────┘   규칙 vs diff 결정적 대조
                                        ▼
                                   report.md   (심각도순 병합)
```

| 단계 | 모듈 | 하는 일 |
|---|---|---|
| 1 | `diff/collector` | git diff → `DiffScope` (변경 라인 + 문맥) |
| 2 | `analysis/clang_tidy` | 활성 프로파일의 체크로 clang-tidy 실행 → 진단 |
| 3 | `llm/postprocess` | LLM triage — 진단 걸러내고, diff에서 결함 직접 탐지, **활성 규칙도 프롬프트로 받아 판정** |
| 3.5 | `conventions/checker` | 규칙을 diff와 **결정적** 대조 (명명=선언 매칭, 구조=AST) → 질문형 finding |
| 4 | `report/markdown` | 심각도순 병합, **커버리지 공백 표시** |

### 규칙이 리뷰에 적용되는 방식

학습된 규칙은 종류에 따라 리뷰에서 다르게 쓰인다.

| 규칙 종류 | 검사 주체 | 재현성 |
|---|---|---|
| **명명** (facet prefix/suffix/casing) | convention checker — 정규식, **결정적** | `reproducible=True` → **CI 막을 수 있음** |
| **구조** (facet=other + `check`) | convention checker — **결정적** (계층은 정규식, 나머지는 AST) | `reproducible=True` → **CI 막을 수 있음** |
| 그 외 facet=other (check 없음) | 리뷰 LLM — 프롬프트에 넣어 판정 | `reproducible=False` → 참고용 |

명명과 구조 규칙은 diff를 **결정적으로** 검사한다. check가 없는 의미적 규칙만 아직 LLM 판단에 머문다.

구조 규칙도 **diff가 실제로 건드린 것**만 본다 — 바뀐 줄에 선언된 함수·멤버·클래스, 새로 추가된
`#include`. 뒤늦게 채택한 규칙이 남의 코드까지 전부 지적하면 아무도 쓰지 않기 때문이다.

> 어떤 check 종류를 결정적으로 강제하는지는 `config.DETERMINISTIC_STRUCTURAL_CHECKS` **한 곳**에서 온다.
> 검사기와 LLM 프롬프트가 각자 목록을 들고 있었을 때, check를 추가하자 같은 지적이 두 번 나왔다.

## 이 구조를 정하는 원칙

- **제안은 열되 채택은 리포가 잠근다** — 규칙 후보가 어떻게 나왔든(통계·추측), 리포 코드가 게이트를 통과시켜야 하고 사람이 승인해야 발효된다.
- **재현 가능한 지적만 CI를 막는다** — 모델 판단은 흔들릴 수 있으므로, finding마다 `Evidence.reproducible`로 구분하고 CI 게이트는 재현 가능한 쪽만 쓴다.
- **못 본 것을 말한다** — 안 읽은 파일·분석 못 한 헤더가 있으면 리포트에 이유와 함께 나열하고, 그런 공백이 있으면 finding이 0건이어도 통과(✅)로 쓰지 않는다.
- **상태는 디렉터리, 이력은 git** — 규칙의 상태·승인·이력을 파일 필드로 재구현하지 않는다.

## 확장 지점

- **리뷰 프로파일** — `profiles.py`에 (clang-tidy 체크 + LLM 지시) 한 벌을 추가하면 새 검사 축이 된다.
- **언어** — C++ 지식이 `languages/cpp/`(ast/parser/naming)에 모여 있어, 두 번째 언어는 `languages/<lang>/`로 나란히 놓으면 된다.
- **추론 check 어휘** — `verifier.py`의 check 종류(위 표)를 늘리면 추론 규칙의 검증 범위가 넓어진다. 구조 check는 `cpp/ast.py` 위에 쌓인다. 어떤 종류를 **늘릴지는 추론이 알려준다**: 모델이 계속 제안하는데 돌릴 검사가 없는 종류가 다음에 만들 것이다 (`base_class`가 그렇게 생겼다).

각 확장의 상세 절차는 [extending.md](extending.md), 결정의 근거는 [design-history.md](design-history.md).
