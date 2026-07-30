# 확장 가이드

## 새 규칙 프로파일 추가

프로파일 하나 = "이 종류의 지적을 하겠다"는 단위입니다. **정의가 한 곳에 모여 있습니다** —
[profiles.py](../src/pumpkins/profiles.py)의 `PROFILES`에 항목을 추가하면 clang-tidy 체크와
LLM 지시문이 함께 붙고, CLI `--profile` 선택지도 자동 생성됩니다.

```python
PROFILES = {
    "memory": Profile(
        name="memory",
        description="소유권·수명 오류",
        clang_tidy_checks=[
            "bugprone-use-after-move",
            "bugprone-dangling-handle",
        ],
        llm_focus="""\
Scan the diff for ownership and lifetime problems clang-tidy misses:
- a reference or pointer outliving the object it refers to
- ...
""",
    ),
}
```

> **왜 한 곳인가.** 예전에는 체크 목록이 `analysis/checks.py`에, 지시문이
> `postprocess.py`의 동시성 전용 시스템 프롬프트에 하드코딩돼 있었습니다. 그래서 프로파일을
> 늘리려면 아무도 안 쳐다보는 프롬프트를 같이 고쳐야 했고, 실제로 그게 안 됐습니다 —
> fmt PR에 돌렸을 때 LLM 출력이 11토큰이었던 이유입니다. 프롬프트가 동시성만 물었고 그 PR엔
> 동시성 코드가 없었으니, 모델은 지시를 정확히 따른 것이었습니다. 정작 실제 결함은
> C++11 리포에 C++17 `inline` 변수를 넣어 CI를 깨뜨리는 것이었습니다.

원칙:

- **clang-tidy 목록은 좁고 정밀하게.** false positive가 적은 체크 위주로. 넓은 판단은 `llm_focus`가 담당.
- **`llm_focus`는 구체적으로.** 막연한 지시는 막연한 지적을 만듭니다. 무엇을 보고할지뿐 아니라
  **무엇을 보고하지 말지도** 쓰세요 — `concurrency`가 *"초기화 후 불변인 데이터는 레이스가
  불가능하니 보고하지 말라"* 를 명시하는 건 실측된 오탐(읽기 전용 `constexpr` 배열을 데이터
  레이스로 지목)을 막기 위해서입니다.
- `clang-analyzer-*` 체크는 컴파일 플래그 의존도가 높아 **얕은 모드에서 특히 부정확** — 넣는다면 compile-DB 모드 검증부터.
- 체크 이름은 clang-tidy 버전에 따라 다름: `clang-tidy --list-checks -checks='*' | grep <keyword>`로 확인.

### 프로파일이 추가 입력을 필요로 할 때

`portability`는 "이 프로젝트가 지원하는 최소 C++ 표준"이 있어야 판단이 성립합니다. 그런 입력은
**모델에게 추론시키지 말고 기계적으로 읽어서 프롬프트에 넣으세요** — 판단의 근거 자체를 모델이
지어내게 하는 셈이 되니까요. `needs_cxx_standard=True`로 표시하면
[analysis/cxx_standard.py](../src/pumpkins/analysis/cxx_standard.py)가 CMakeLists와 CI 매트릭스에서
**가장 낮은 선언값**을 읽어 프롬프트에 붙입니다(그게 계속 컴파일돼야 하는 값이므로).

선언이 없으면 기본값을 가정하지 않고 "선언 없음"이라고 알립니다. 최소 표준을 잘못 잡으면
모든 현대적 문법이 오탐이 되기 때문입니다.

### 프롬프트 조립

[postprocess.py](../src/pumpkins/llm/postprocess.py)의 `build_system_prompt()`가 매 실행마다
세 조각을 합칩니다:

1. **공통** (`_BASE_PROMPT`) — 역할, triage 규칙, 출력 계약
2. **프로파일** — `profile.llm_focus`가 "FOCUS" 절로, 필요하면 최소 C++ 표준 절이 앞에
3. **저장소 규칙** — `pumpkins/rules/`의 활성 규칙

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

## tree-sitter 가 없는 환경

구조 검사는 tree-sitter(네이티브 라이브러리)에 기댑니다. 없거나 ABI가 어긋나면
`languages/cpp/ast.py`의 임포트 가드가 잡아 정규식 스캐너로 내려갑니다. **다만 조용히
내려가지 않습니다** — 폴백은 통계를 바꾸고 통계는 규칙을 바꾸므로, 결과가 달라졌는데
아무 말도 안 하는 건 리뷰 리포트에서 고쳤던 바로 그 실패이기 때문입니다.

| 어디 | 무엇을 하는가 |
|---|---|
| 실행 중 | `cpp_ast.require(용도)` 가 용도마다 한 번 경고 + 복구 명령 안내 |
| `run.json` / `learn-report.yml` | `ast_engine` / `scan.engine` 에 `tree-sitter` 또는 `regex-fallback` 기록 |
| 테스트 | 없으면 **실패**. 의도적으로 건너뛰려면 `PUMPKINS_ALLOW_NO_TREE_SITTER=1` |

테스트가 기본 실패인 이유: 한 환경에서 이 파일 10개가 통째로 skip 된 채 "전부 통과"로
보고된 적이 있습니다. 선언된 필수 의존성이 없는 건 정상 상태가 아닙니다.

**설정으로 어디까지 막을 수 있나.** 가장 흔한 실패는 두 패키지의 버전이 어긋나는
것이고, 그건 `pyproject.toml`의 상한으로 막힙니다. 반면 "이 플랫폼용 미리 만든 패키지가
없고 컴파일러도 없다"는 설정으로 못 막습니다 — 빌드 도구가 필요합니다. 주요 플랫폼에는
미리 만든 패키지가 있어 드문 경우이고, 그때도 도구는 죽지 않고 내려가되 **말은 합니다.**

## 언어 문법 건드리기 (`languages/`)

C++를 읽는 층이 셋으로 갈려 있습니다(`extractor.py`는 통계만 담당):

- **`cpp/ast.py`** (tree-sitter) — learn 스캔 + 구조 검증의 **주 파서**. 식별자 카테고리, 함수 반환 타입,
  멤버 소유권(raw/smart), 클래스 상속이 여기서 나옵니다.
- **`cpp/parser.py`** (정규식) — 리뷰 hunk 검사 + AST 폴백. 예약어·접근 지정자·`#include` 추출도 여기.
- **`naming.py`** — 이름 facet 어휘(어떤 접두사/casing이 규칙 후보인가). "이름을 어떻게 분해하나"는 여기서.

### 전처리기가 없다는 것의 의미

tree-sitter는 매크로를 모릅니다. `class FOO_API Bar`를 만나면 매크로를 클래스 이름으로 읽고, 그러면
**그 클래스의 멤버가 통째로 사라집니다**(본문이 함수 본문으로 처리되어 멤버가 지역변수가 됨).
yaml-cpp에서 판정 못 한 class 선언 25곳, `private_member` 92 → 124(+35%)였습니다.

세 가지 모양이 **각각 다르게** 실패합니다:

| 코드 | 파서 반응 | 무엇으로 고치나 |
|---|---|---|
| `class M Foo : Base { … }` | ERROR 노드 | **트리가 증명** — 복구 가능 |
| `class M Foo { … }` | **오류 없음**. function_definition으로 조용히 오독 | **트리가 증명** — 선언자가 맨 식별자인 함수 정의는 유효한 C++이 아님 |
| `class M Foo;` | 오류 없음. **유효한 C++ `class Foo bar;`와 완전히 동일한 트리** | 증명 불가 — **리포의 `#define` 목록**이 유일한 근거 |

세 번째 줄이 설계를 결정합니다. 문법이 진짜로 모호하므로 **텍스트 모양으로는 원리적으로 못 풉니다.**
그래서 이름 모양(대문자 등)으로 찍지 않고 **리포가 스스로 선언한 `#define`을 읽습니다**
(`collect_macros` → `MacroTable`). 추측 대신 리포에 물어보는 것 — 규칙을 다루는 방식과 같은 태도입니다.

읽기는 두 단계이고, **각각 오류가 줄어들 때만 채택**됩니다:

1. **매크로 펼치기 (`MacroTable.expand`)** — 리포가 정의한 매크로를 **파일 전체에서** 본문으로 치환합니다.
   class 헤더만 고치는 것보다 낫다는 게 실측으로 나왔습니다 (yaml-cpp: 파싱 오류 337 → 76):

   | class 헤더만 고칠 때 통계에 남던 것 | 전체 치환 후 |
   |---|---|
   | `function 'string FpToString'` | `function 'FpToString'` ✓ |
   | `function 'vector<Node> LoadAll'` | `function 'LoadAll'` ✓ |
   | `public_field 'override'` ×14 | 사라짐 (키워드) |
   | `public_field 'JKJ_CONSTEXPR14'` ×4 | 사라짐 (매크로 이름) |

   **전처리기 지시문 줄(`#...`)은 건드리지 않습니다.** 헤더는 전부 `#ifndef GUARD` / `#define GUARD`로
   시작하고, 가드를 빈 문자열로 펼치면 `#ifndef`만 남습니다 — 시도했을 때 97개 중 **49개 파일에
   없던 오류가 새로 생겼습니다.**

2. **트리 근거 복구** — 리포가 정의하지 **않은** 매크로(컴파일러 제공, 또는 스캔에서 제외된 서드파티
   헤더)를 위해 남겨둡니다. 이름을 몰라도 트리가 오독을 증명하는 경우가 있으니까요.

두 단계 모두 **오류 수가 줄어들 때만** 결과를 채택합니다. 이 불변식 덕분에 리포별 허용목록 없이
모든 파일에 그냥 돌릴 수 있습니다 — 복구가 파싱을 더 나쁘게 만들 수는 없습니다. 치환은 줄 수를
바꾸지 않고(본문은 한 줄), 공백 치환도 같은 길이를 유지합니다 — 줄 번호가 밀리면 구조 지적이
엉뚱한 코드를 가리키니까요.

**여전히 전처리기는 아닙니다.** 함수형 매크로(`#define MIN(a,b) …`)와 `\`로 이어진 정의는 펼치지
않고, 조건부 컴파일(`#if`)도 해석하지 않습니다. 그래도 못 읽은 자리는 아래처럼 세어서 보고합니다.

### 그래도 못 읽은 것은 세어서 말합니다

복구해도 판정 못 하는 자리가 남습니다(모르는 매크로이거나, 진짜 `class Foo bar;`이거나).
`ScanHealth`가 그걸 세서 learn 출력과 `learn-report.yml`의 `scan.parse_health`에 남깁니다.

**두 숫자를 구분하세요:**

- **판정 못 한 class 선언** — 통계에 직접 영향. **0이 아니면 경고합니다.**
- **파싱 오류 구간** — 대부분 복잡한 템플릿(SFINAE 등). 통계에 영향 없음. **경고하지 않습니다.**

yaml-cpp에서 앞은 25 → 0, 뒤는 337 → 76입니다. 남은 76곳은 진짜 문법 한계(SFINAE 등)인데 클래스는
전부 정상 추출됩니다. 여기에 경고를 걸면 **진지한 C++ 리포마다 항상 켜져 있는 경고**가 되고, 항상 켜진
경고는 아무도 안 읽어서 정작 중요한 하나를 묻어버립니다.

이 측정이 있는 이유가 곧 이 버그의 교훈입니다: **도구가 틀렸는데 아무 말도 안 했고**, 사람이 출력을
눈으로 훑다가 우연히 발견했습니다. 다음 파싱 버그는 첫 실행에서 숫자로 드러나야 합니다.

새 카테고리를 추가할 때는 `CATEGORIES`(extractor.py) + `cpp_ast.scan`의 분기(주) + `cpp_parser`(리뷰 hunk·폴백)를
함께 고치고, **하위 호환을 반드시 생각하세요** — 이미 승인·커밋된 규칙이 새 카테고리 이름을 모르면 조용히 죽습니다.
`checker._CATEGORY_ALIASES`가 그 장치입니다(옛 이름 → 새 이름들). 넓은 쪽에서 좁은 쪽으로만
매핑하고, 반대 방향은 만들지 마세요: 정보가 없을 때 추측하는 규칙이 됩니다.

두 번째 언어가 필요해지면 이 파일이 복제 지점입니다. **지금 플러그인 인터페이스를 만들지 마세요** —
구현체가 하나뿐인 추상화는 두 번째가 왔을 때 대개 안 맞습니다.

## 확장자 매핑 (`languages/__init__.py`)

리포가 `pumpkins/settings.yml`로 덮어씁니다. 새 축(예: 언어별 파서 선택)을 넣으려면 그 파일의
스키마를 확장하세요. 두 가지를 지키세요:

- **`pumpkins/learn-report.yml`에 넣지 마세요** — `learn`이 매번 덮어써서 손으로 적은 설정이 사라집니다.
  같은 디렉터리 안이지만 소유자가 다릅니다: `learn-report.yml`은 learn이, `settings.yml`은 사람이 씁니다.
- **추가한 확장자를 TU 집합에 자동 편입시키지 마세요** — 헤더를 clang-tidy에 단독으로 먹이면
  전부 컴파일 에러가 됩니다.

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

## 추론 check 추가 (`conventions/verifier.py`)

`--infer`는 규칙을 추측한 뒤 **check로 검증**합니다(추측 → 리포에 대조 → 게이트). 새 종류의 규칙을
기계 검증하려면 check 어휘를 넓히세요:

1. `learner.py`의 `RuleCheck`에 새 `kind`와 파라미터를 추가하고, 추론 프롬프트(`_INFER_SYSTEM`)에
   그 kind를 언제·어떻게 채우는지 **예시**를 넣으세요 (모델이 자기 규칙과 맞는 check를 붙이도록).
2. `verifier.py`의 `verify()`에 그 kind를 리포 전체에 대조해 `CheckResult(matches, total)`를 내는
   분기를 추가하세요. `verify_inferred`가 나머지(게이트·분류)를 처리합니다.
3. 리뷰에서 **결정적으로** 검사하려면 `config.DETERMINISTIC_STRUCTURAL_CHECKS`에 kind를 넣고
   [checker.py](../src/pumpkins/conventions/checker.py)의 `_violations()`에 분기를 추가하세요.
   AST가 필요하면 `DETERMINISTIC_STRUCTURAL_AST`에도 넣습니다.
   그 목록을 안 고치면 LLM 프롬프트가 그 규칙을 계속 들고 있어 **같은 지적이 두 번** 나옵니다 —
   검사기와 프롬프트가 각자 목록을 갖고 있었을 때 실제로 그랬고, 그래서 지금은 config 한 곳에서 옵니다.

**분모를 먼저 정하세요.** check 하나를 만들 때 가장 자주 틀리는 건 세는 방법이 아니라 *무엇으로
나눌지*입니다. 소유권 규칙의 분모는 전체 멤버가 아니라 **포인터를 쥔 멤버**고, 계층 규칙의 분모는
레포 전체가 아니라 **그 계층의 파일**입니다. 규칙이 말하는 모집단보다 분모가 넓으면 어떤 주장이든
조용히 참이 됩니다 — 이 프로젝트가 이미 두 번 치른 값입니다.

원칙은 게이트와 같습니다: 실패 방향을 안전하게(검증 실패 = 기각, 틀린 규칙 채택 아님).

**어떤 check를 다음에 만들지는 추론이 알려줍니다.** 모델이 반복해서 제안하는데 돌릴 검사가 없는
종류가 곧 다음 후보입니다 — `base_class`("예외 클래스는 X를 상속한다")가 실제로 그렇게 생겼습니다.

**구조 check**는 [cpp/ast.py](../src/pumpkins/languages/cpp/ast.py)(tree-sitter AST) 위에 쌓습니다.
tree-sitter는 정식 의존성이지만 **네이티브**라, ABI가 깨지면 `verify()`가 `None`을 돌려 그 check만
미검증으로 저하됩니다(무관한 명령은 안 죽음). 그래서 **AST 없이 되는 check는 AST에 기대지 마세요** —
계층 방향(`include_direction`)이 정규식으로 도는 이유입니다. `#include`는 한 줄로 읽히고, 파급이 가장 큰
규칙을 네이티브 빌드 실패로 잃을 이유가 없습니다.

## LLM 관련 조정 포인트

| 조정 | 위치 | 비고 |
|---|---|---|
| 모델 변경 | CLI `--model` 또는 `config.PROVIDER_MODELS` (`default_review_model()` / `default_learn_model()`) | 프로바이더별 기본값 — anthropic이면 리뷰 `claude-opus-4-8`, 학습 `claude-sonnet-5`. 단계별 근거는 [설계 문서 §4](convention-detection-design.md) |
| 프롬프트 | `llm/postprocess.py` `_BASE_PROMPT` + `build_system_prompt()` | 공통·프로파일·저장소 규칙 세 조각으로 조립. 실패 시나리오를 구체적으로 쓰게 하는 문구가 핵심 |
| 샘플링 온도 | `config.REVIEW_TEMPERATURE` / `LEARN_TEMPERATURE` | 리뷰는 `0.0` 고정(사용자에게 바로 가는 출력), 학습은 `None`(프로바이더 기본값 — 임계선·승인 게이트가 뒤에 있음). `None`은 파라미터를 **아예 보내지 않는다**는 뜻 — 일부 모델이 이 파라미터를 거부하므로 |
| 출력 스키마 | `_Verdict`, `_ExtraFinding`, `_LlmReview` | Pydantic 모델 수정만으로 스키마 강제 유지 |
| 대형 diff 청킹 | `process()` 호출 전 `DiffScope` 분할 | 파일 단위 분할 → 호출 병렬화 순서로 |

## 상수 (config.py)

| 상수 | 기본값 | 의미 |
|---|---|---|
| `LINE_FILTER_MARGIN` | 15 | 변경 범위 확장폭 — 동시성 문맥 확보용. 노이즈가 늘면 줄일 것 |
| `SHALLOW_MODE_STD` | c++17 | 얕은 모드 기본 표준. 대상 레포에 맞게 CLI 옵션화 가능 |
| `COMPILE_DB_CANDIDATES` | `.`, `build`, `out`, ... | compile_commands.json 탐색 경로 |
| C++ 확장자 | `cpp_parser.EXTENSIONS` (+ 리포별 `pumpkins/settings.yml`) | diff에서 C++로 취급할 확장자 — config.py가 아니라 **언어 파서**에 있음. 여기 없는 확장자는 `skipped_files`로 빠져 리포트에 명시됨 |
| `MIN_RULE_OCCURRENCES` / `MIN_RULE_CONSISTENCY` | 20 / 0.85 | 컨벤션 채택 임계선. **분모가 맞을 때만 안전하다** — [설계 문서 §5.5](convention-detection-design.md) |
| `LEARN_SKIP_DIRS` | `third_party`, `vendor`, ... | learn이 절대 읽지 않는 디렉터리 |
| `LEARN_TEST_DIRS` | `test`, `tests`, ... | 기본 제외, `learn --include-tests`로 해제. 벤더링된 테스트 프레임워크가 통계를 뒤집은 실측이 근거 |
