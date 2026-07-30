# pumpkins

> ⚠️ **초기 프로토타입입니다.** 아직 제품이 아니라, 아래 아이디어가 실제로 통하는지 확인하는 실험용 CLI입니다.

**사람 리뷰어가 하던 일을 대신 해주는 코드 리뷰 도우미.**
코드 변경분을 보고 — ① 이 프로젝트가 지켜온 **코드 규칙(컨벤션)** 을 어겼는지, ② **기능적으로 문제**가 생길 만한 곳은 없는지 예측해서 — *"야, 여기 이렇게 해야 하는 거 아니야? 이거 잘못한 것 같은데?"* 라고 콕 집어 알려주는 것을 목표로 합니다.

## 무엇을 잡으려 하는가

사람이 리뷰할 때 눈으로 보던 것들을 대신 봅니다.

1. **프로젝트별 코드 규칙 / 컨벤션**
   - 멤버 변수엔 `m_` 접두사, 함수 이름 첫 글자는 소문자, 파일마다 통용되는 명명·구조 규칙 등.
   - 규칙이 리포에 명문화돼 있지 않더라도 **기존 코드에서 관행을 읽어내** 어긋난 곳을 지적하는 것이 목표.
2. **기능적 문제 예측**
   - 이 변경이 런타임에 사고를 낼 만한 지점(예: 동시성 안티패턴).
   - 숙련된 리뷰어가 *"이거 이렇게 하면 터질 것 같은데?"* 하고 감지하는 그 감각.

→ 핵심 가치는 **"사람이 리뷰에서 하던 판단을 대신 던져준다"** 는 것입니다. 정적 분석기 한 대가 아니라, 리뷰어처럼 말을 걸어주는 도우미를 지향합니다.

## 지금 구현된 것

**① 기능적 문제(동시성) 리뷰 파이프라인** — `pumpkins`

git diff → clang-tidy 정적 분석 → LLM(Claude/GPT 선택 가능) 후처리(노이즈 제거·심각도 판정·설명/수정안 생성) → 마크다운 리포트.

- **clang-tidy는 정밀한 그물, LLM은 diff를 직접 읽어 규칙·패턴을 잡는 넓은 그물** 역할. 리뷰어처럼 판단하는 몫은 LLM이 맡습니다.
- 변경 라인 주변(±15줄)만 봅니다. 대상 프로젝트를 **빌드하지 않아도 동작** 하는데, 이건 도입 마찰을 낮추기 위한 선택이지 그 자체가 목적은 아닙니다.
- **지적마다 출처 라벨이 붙습니다.** *"어떤 규칙 때문에 이 말을 했나"* 와 *"다시 돌리면 또 나오나"* 를 finding마다 기록합니다 — `규칙 \`member-prefix-m\` · convention · 재현 가능 · 근거 187개 중 92%`. 규칙 대조는 항상 같은 결과를 내지만 LLM 판단은 흔들리므로, **CI를 막을 수 있는 것은 재현 가능한 지적뿐**입니다. 리포트 상단에 그 개수가 요약됩니다.
- **못 본 것을 말합니다.** 확장자 때문에 안 읽은 파일과 정적 분석이 불가능했던 헤더를 리포트에 이유와 함께 나열하고, 그런 공백이 있으면 finding이 0건이어도 `✅`를 쓰지 않습니다. "검사했는데 깨끗함"과 "검사를 못 함"이 구분되지 않는 초록 체크가 리뷰 도구에서 가장 위험하기 때문입니다.

**② 컨벤션 학습 + 지적 (MVP 1·2단계)** — `pumpkins learn` → 리뷰에 자동 연결

리포 스캔 → 식별자 명명 통계(기계적 추출 — LLM엔 통계 요약만 전달) → LLM 규칙 판정 → 임계선 게이트(20회+/85%+) → 사람이 검수·커밋하는 **`pumpkins/` 규칙 저장소** 생성.

`learn`은 **필요할 때 한 번**(리포가 크게 바뀌면 다시), `review`는 **모든 PR에** 돌리는 구조입니다. 리뷰는 규칙 파일을 읽기만 하므로 규칙 생성 비용이 0입니다.

이후 리뷰(`pumpkins`) 실행 시 `<repo>/pumpkins/`의 **활성 규칙**과 diff를 대조해 **질문형으로 지적**합니다 — *"`running` — 멤버 변수는 `m_` 접두사를 사용한다 관행과 다른 것 같아요. 여기만 다르게 한 이유가 있을까요?"* (근거 수치 + 적용 범위 + rename 제안 포함). 이 대조는 결정적이라 **API 키 없이도 동작**합니다.

규칙마다 **적용 범위(`scope`)** 를 둘 수 있어서 레거시·생성 코드 트리는 자기 관행을 유지합니다 — 아래 [적용 범위 정하기](#적용-범위-정하기--폴더확장자별로-다른-규칙) 참고.

## 문서

| 문서 | 내용 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | **현재 구조** — 두 국면(learn/review), 모듈 지도, 데이터 계약, 규칙이 적용되는 방식. 구조만, 추상적으로 |
| [docs/convention-detection-design.md](docs/convention-detection-design.md) | **핵심 기능 설계 검토** — 사람 리뷰어의 지적을 자동화하는 방안(A/B/C), 설계 결정, 리스크, MVP 경로 |
| [docs/design-history.md](docs/design-history.md) | **시행착오·측정 기록** — 실제 리포에서 발견해 고친 것, 판단의 근거. "왜 이렇게 됐나"의 히스토리(구조 아님) |
| [docs/verification-plan.md](docs/verification-plan.md) | "리뷰어의 판단을 재현할 수 있는가" 검증 방법, 측정 지표, 채택/기각 기준 |
| [docs/extending.md](docs/extending.md) | 규칙(체크) 프로파일·diff 소스·분석기·리포트 포맷 확장 방법 |
| [docs/llm-provider-and-keys-design.md](docs/llm-provider-and-keys-design.md) | **LLM 프로바이더 & API 키** — 프로바이더 선택·모델·키 관리의 현재 동작 레퍼런스 |

## 아키텍처

**두 국면**으로 나뉩니다 — 규칙을 만드는 `learn`, 규칙을 쓰는 `review`. 둘은 규칙 저장소(`pumpkins/`)로만 연결됩니다.

```
learn:   리포 코드 ──▶ 규칙 추출 ──▶ candidates/ ──(사람 승인)──▶ rules/
review:  git diff  ──▶ clang-tidy + LLM triage + 컨벤션 검사 ──▶ report.md   (rules/ 를 읽어 적용)
```

전체 모듈 지도·데이터 계약·리뷰 단계는 **[docs/architecture.md](docs/architecture.md)** 를 보세요.

## 빠른 시작

처음이라면 아래 순서를 그대로 따라 하면 됩니다.

### 0. 요구사항 확인

- **Python ≥ 3.10** — 확인: macOS/Linux `python3 --version`, **Windows `python --version`** (또는 `py --version`).
  - ⚠️ Windows에서 `python3`는 Microsoft Store 별칭이라 "Python was not found" 오류가 납니다. 실제 파이썬은 `python` 또는 `py`로 부르세요. (이 README의 이후 `python3` 명령은 Windows에서 모두 `py`로 바꿔 읽으면 됩니다.)
- **clang-tidy** (PATH에 있어야 함) — `clang-tidy --version`
  - **가장 간단(OS 공통, 권장)**: 아래 3단계에서 venv를 켠 뒤 `pip install clang-tidy` — 바이너리가 번들돼 있어 시스템 설치가 필요 없고 Windows·Linux·macOS 모두 동일합니다.
  - 시스템 패키지로 깔려면 — macOS: `brew install llvm`(설치 후 PATH 추가), Ubuntu/Debian: `sudo apt install clang-tidy`, **Windows: `winget install LLVM.LLVM`** (설치 후 새 터미널).

가상환경 생성 방법은 2단계에서 환경에 맞게 고르면 됩니다.

### 1. 저장소 클론

```bash
git clone <이 저장소 URL> pumpkins
cd pumpkins
```

### 2. 가상환경(venv) 생성 및 활성화

프로젝트 전용 파이썬 환경을 만들어 의존성을 시스템과 격리합니다. 아래 **A / B 중 하나**로 `.venv/` 폴더를 만드세요.

**방법 A — 표준 `venv`** (권장)

```bash
# macOS / Linux
python3 -m venv .venv
# Windows (python3 는 Store 별칭이라 실패 — python 또는 py 사용)
py -m venv .venv
```

> Debian/Ubuntu·WSL에서는 `venv`가 별도 패키지라 이 명령이 실패할 수 있습니다. 그럴 땐 `sudo apt install python3-venv` 후 다시 실행하거나, 아래 방법 B를 쓰세요.

**방법 B — `virtualenv`** (sudo 없이, WSL/Ubuntu에서 검증된 방법)

```bash
pip install --user virtualenv   # 한 번만
virtualenv .venv
```

**생성한 뒤 활성화** (A/B 공통) — OS·셸에 따라 경로가 다릅니다. Windows는 `bin/`이 아니라 `Scripts/`입니다.

```bash
# macOS / Linux
source .venv/bin/activate
```

```powershell
# Windows — PowerShell
.venv\Scripts\Activate.ps1
#   ↳ "이 시스템에서 스크립트를 실행할 수 없으므로"(execution policy) 오류가 나면,
#      현재 세션에만 허용하고 다시 실행:
#      Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   ↳ 정책을 건드리기 싫으면 cmd 스타일 배치로: .venv\Scripts\activate.bat
```

```bash
# Windows — Git Bash (bin 이 아니라 Scripts)
source .venv/Scripts/activate
```

활성화되면 프롬프트 앞에 `(.venv)`가 붙습니다. 끝낼 때는 어느 셸에서든 `deactivate`.

> `.venv/`는 커밋하지 않습니다(`.gitignore`에 포함). 사람마다 각자 로컬에 만듭니다.

### 3. 패키지 설치 (개발 모드)

```bash
# pip 최신화 후 개발 의존성까지 설치 (-e: 소스 수정이 바로 반영되는 editable 설치)
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

### 4. LLM 프로바이더 & API 키 설정

Claude(Anthropic)와 GPT(OpenAI) 중 하나를 골라 씁니다. 키는 **환경변수로만** 전달합니다 (코드에 하드코딩 금지). 가장 쉬운 방법은 `.env` 파일:

```bash
# macOS / Linux / Windows(PowerShell·Git Bash) 모두 cp 동작
cp .env.example .env
# ↳ Windows cmd.exe 라면: copy .env.example .env
# .env를 열어 LLM_PROVIDER와 쓰는 쪽 키를 채우세요:
#   LLM_PROVIDER=anthropic   (또는 openai)
#   ANTHROPIC_API_KEY=sk-ant-...
#   OPENAI_API_KEY=sk-...
```

`.env`는 `.gitignore`에 등록돼 있어 커밋되지 않습니다 — 사람마다 각자 만듭니다. CLI가 실행 시 현재 디렉터리의 `.env`를 자동으로 읽습니다.

셸에서 직접 환경변수를 지정해도 됩니다 (이 값이 `.env`보다 **우선**합니다 — CI나 일시적 전환에 유용). 셸마다 문법이 다릅니다:

```bash
# macOS / Linux / Git Bash
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-...
```

```powershell
# Windows — PowerShell
$env:LLM_PROVIDER = "openai"
$env:OPENAI_API_KEY = "sk-..."
```

> 설계 배경(전환 로직·키 관리 원칙)은 [docs/llm-provider-and-keys-design.md](docs/llm-provider-and-keys-design.md).

### 5. 설치 확인

```bash
pumpkins --help   # CLI가 잡히면 성공
pytest            # 테스트 통과 확인
```

여기까지 되면 준비 끝 — 아래 "실행"으로 넘어가세요.

## 실행

```bash
# 대상 레포의 워킹트리 변경분(uncommitted) 리뷰
pumpkins --repo /path/to/cpp/project

# PR처럼: main 기준 브랜치 변경분 리뷰, 파일로 출력
pumpkins --repo /path/to/cpp/project --base main --out report.md

# LLM 없이 clang-tidy 원본 결과만 (파이프라인 디버깅용)
pumpkins --repo /path/to/cpp/project --no-llm -v

# 리포트 + 디버깅 산출물을 현재 경로의 out/ 에 남김 (대상 레포로 이동할 필요 없음)
pumpkins --repo /path/to/cpp/project --out-dir
pumpkins --repo /path/to/cpp/project --out-dir 그밖의경로
```

### `--out-dir` — 실행 산출물 한곳에 모아 보기

```
out/
├── report.md          렌더된 리포트 (stdout과 동일)
├── run.json           실행 출처: 프로바이더·모델·clang-tidy 버전·규칙 지문·커버리지
├── diff.patch         수집된 diff 원본 (파이프라인이 실제로 본 것)
├── diagnostics.json   clang-tidy 원본 진단 (triage 이전)
├── findings.json      최종 finding, 구조화
└── llm/
    ├── request.txt    LLM에 보낸 프롬프트 전문
    └── response.json  LLM이 돌려준 구조화 출력
```

`--out-dir`만 주면 `./out/`, 경로를 주면 그곳에 씁니다. 이전 실행의 산출물은 지우고 쓰므로 섞이지 않습니다.

**`llm/request.txt`가 디버깅 가치가 가장 큽니다.** *"왜 이런 지적을 했지"* 또는 *"왜 아무 말도 안 하지"* 의 답이 대개 프롬프트에 그대로 적혀 있습니다 — 예를 들어 지금 시스템 프롬프트는 `You are a senior C++ reviewer specializing in concurrency bugs`로 시작하므로, 동시성 코드가 없는 PR에 아무 말도 안 하는 게 정상입니다.

**`run.json`은 편의 기능이 아니라 제품 주장의 근거입니다.** "어떤 모델에서도 재현 가능하고 근거 있는 리뷰"라고 말하려면 무엇으로 돌렸는지가 결과와 함께 남아야 합니다 — 모델을 바꿨을 때 차이가 모델 때문인지 규칙 때문인지 도구 버전 때문인지 가려야 하기 때문입니다.

```json
{
  "llm": { "used": true, "provider": "openai", "model": "gpt-4o", "temperature": 0.0 },
  "clang_tidy_version": "14.0.0",
  "conventions": { "active_rules": 2, "pending_candidates": 1,
                   "rules_fingerprint": "sha256:50f64bfbd136b9a9" },
  "coverage": { "analyzed_files": 1, "skipped_headers": ["include/fmt/format.h"],
                "complete": false }
}
```

`rules_fingerprint`는 활성 규칙의 내용 해시입니다(미승인 후보는 제외 — 적용되지 않으니 실행의 정체성에 들어가지 않습니다). git SHA와 달리 커밋 전에도 존재하고, 두 실행의 규칙이 같았는지를 바로 답합니다.

컨벤션 학습 → 지적:

```bash
# 1) 리포의 명명 관행을 학습 — 규칙 후보가 pumpkins/candidates/에 생깁니다
pumpkins learn --repo /path/to/cpp/project

# 2) 검수하고 승인 (승인 전까지는 리뷰에 적용되지 않습니다)
git mv pumpkins/candidates/function-casing-lowerCamel.yml pumpkins/rules/

# 3) 이후의 리뷰는 pumpkins/rules/를 자동으로 대조 (API 키 없이도 동작)
pumpkins --repo /path/to/cpp/project --no-llm

# LLM 없이, LLM에 전달될 통계 원본만 출력 (learn 디버깅용)
pumpkins learn --repo /path/to/cpp/project --no-llm

# 이 리포만의 규칙을 AI가 추측 (틀 없이 코드를 읽음) — 추측을 레포에 대조·채점해 채택
pumpkins learn --repo /path/to/cpp/project --infer
```

**통계는 못 잡는 이 리포만의 규칙 — `--infer`.** 기본 `learn`은 내가 정한 틀(접두사·casing 등)에 맞는
명명 규칙만 찾습니다. `--infer`는 틀 없이 코드를 읽어 *"이 리포가 지키는, 보편 C++이 아닌 관행"* 을
**추측**하고 — 계층 방향, 소유권, 상속, `#pragma once` 같은 것들 — 그 추측을 **레포 전체에 대조해 실측
coverage로 채점**합니다. 틀린 추측은 coverage로 기각되고, 통과분만 `candidates/`에 오릅니다.
코드를 보내므로 토큰이 더 들어 opt-in이며, 승인 게이트는 그대로입니다.

**비싼 모델에게 리포 전체를 읽히지 않습니다.** 두 번에 나눠 봅니다:

1. **훑기** — LLM 없이 뽑은 *구조 요약*(디렉터리, 디렉터리 간 include 방향, 클래스와 멤버 타입)을
   **싼 모델**이 읽고 *"어디를 열어봐야 하는지"* 만 고릅니다. 짚이는 곳이 없으면 비싼 모델은 아예 안 돕니다.
2. **정독** — **강한 모델**이 지목된 파일 몇 개만 전문으로 읽고 규칙을 씁니다.

yaml-cpp(97개 파일) 실측: 훑기 ~2천 토큰으로 6개 파일을 골라냈고, 거기서 나온 계층 규칙이
**100%(38/38)로 검증**되어 승인 후 리뷰가 위반을 결정적으로 잡았습니다. 같은 실행에서
"모든 멤버는 `m_` 접두사"라는 과한 추측은 **53%로 기각**됐습니다.

`--no-triage`로 훑기를 끄면 예전처럼 경로 순서대로 읽습니다.
설계 배경은 [설계 문서 §2](docs/convention-detection-design.md).

컨벤션 관련 옵션: `--conventions PATH`(기본: `<repo>/pumpkins/`, 없으면 레거시 `conventions.yml`), `--no-conventions`(대조 끄기).

### 확장자 판정을 프로젝트에 맞추기

도구는 확장자를 보고 "이건 C++이구나" 판단하는데, 프로젝트마다 관행이 다릅니다. 필요할 때만
`pumpkins/settings.yml`을 만들면 됩니다 (안 만들면 기본 목록으로 동작).

```yaml
languages:
  cpp:
    extra_extensions: [".ipp", ".tcc"]   # 이 프로젝트에선 이것도 C++
    exclude_extensions: [".inl"]         # 이건 아님
```

`.h`가 C인지 C++인지, `.inc`가 소스인지 생성 데이터인지는 프로젝트마다 다릅니다. 실제로 `.tc`를
YAML 테스트케이스로 쓰는 리포를 만났는데, 전역 목록으로는 그걸 소스로 오독합니다.

추가한 확장자가 clang-tidy 단독 분석 대상이 되지는 않습니다 — `.ipp`를 추가하는 건 "헤더가 더
있다"는 뜻이지 "`.cpp`가 더 있다"는 뜻이 아니니까요.

### 규칙 저장소 — `conventions/`

```
pumpkins/
├── settings.yml       사람이 씁니다 — 확장자 판정 등. learn이 건드리지 않습니다
├── learn-report.yml   learn이 씁니다 — 지난 학습이 무엇을 보고 무엇을 버렸는지
├── rules/             활성 — 리뷰가 적용하는 것은 여기뿐
├── candidates/        판단 대기 — 리뷰에 영향 없음
└── archive/           기각·은퇴 — learn이 다시 제안하지 않습니다
```

디렉터리 이름을 도구 이름으로 둔 이유: `conventions` 같은 일반명사는 프로젝트가 자기 문서나
네임스페이스에 이미 쓸 수 있어 부딪힙니다. 남의 프로젝트에 심는 디렉터리는 자기 이름을 써야 합니다.

**파일이 둘로 나뉜 기준은 누가 쓰는가입니다.** `learn-report.yml`은 학습할 때마다 새로 쓰이는
기록이라, 사람이 손으로 적은 값을 거기 두면 다음 학습에서 사라집니다. 손으로 적을 값은
`settings.yml`에 두세요 — learn은 이 파일을 절대 건드리지 않습니다.

**규칙의 상태는 파일이 놓인 디렉터리입니다.** 파일 안에 `status:` 필드를 두지 않았기 때문에 상태와 실제가 어긋날 수 없고, 상태 전이가 `git mv` 한 번이라 **누가 언제 승인했는지를 git이 자동으로 기록**합니다. 승인자 필드를 손으로 관리할 필요가 없습니다.

**이력도 git이 관리합니다.** 규칙 하나가 파일 하나이므로:

```bash
git log --follow pumpkins/rules/member-prefix-m.yml   # 이 규칙의 전체 이력
```

파일 안에 `history:` 배열을 두는 건 작성자 신원도 서명도 없는 git 재구현이라 하지 않았습니다. 파일에는 git이 줄 수 없는 것 — **결정의 이유(`reason`)** — 만 남깁니다.

### learn을 다시 돌리면

**검수한 결정이 사라지지 않습니다.** learn은 기존 상태를 읽고 병합하며, 활성 규칙을 덮어쓰지 않습니다.

| 상황 | 동작 |
|---|---|
| 새 규칙 후보 | `candidates/`에 씀 |
| 활성 규칙과 동일 | 근거 수치만 갱신 — 결정과 `reason`은 보존 (리포가 커진 건 새 결정이 아님) |
| `archive/`에 기각돼 있음 | **다시 제안하지 않음.** 요약에 한 줄만 (`--reconsider`로 재검토) |
| 같은 category/facet인데 값이 달라짐 | 조용히 뒤집지 않고 **대체 제안**으로 — 무엇을 대체하는지 파일에 적힘 |
| 활성인데 새 스캔이 뒷받침 못함 | **은퇴 후보**로 보고만 함. 자동 삭제 안 함 |

기각을 기억하는 게 핵심입니다 — 기각은 "없음"이 아니라 **"안 하기로 한 결정"** 이고, 매번 같은 걸 다시 제안하면 사용자는 곧 전부 무시하게 됩니다.

첫 실행을 그대로 받아들이겠다면 `--accept-all`로 `rules/`에 바로 쓸 수 있습니다.

### 적용 범위 정하기 — 폴더·확장자별로 다른 규칙

리포 하나에 관행이 하나인 경우는 드뭅니다. 레거시 트리, 코드 생성 산출물, 벤더링된 의존성은 규칙이 다른 게 정상입니다.
그래서 **학습이 읽는 범위**와 **규칙이 판정하는 범위**가 같은 어휘를 씁니다.

```bash
# 특정 트리만 학습 → 나온 규칙에 그 범위가 scope로 자동 기록됨
pumpkins learn --repo . --include 'src/core/**'

# 생성 코드·벤더 트리 제외 (반복 지정 가능)
pumpkins learn --repo . --exclude 'model/generated/**' --exclude 'src/legacy'

# 테스트 디렉터리는 기본 제외 — 포함하려면 명시
pumpkins learn --repo . --include-tests
```

> 테스트 디렉터리를 기본에서 뺀 이유는 실측입니다. fmt에 그냥 돌리면 번들된 `test/gtest/`(googletest)가 UpperCamel 식별자 2845건 중 2837건을 공급해서, 전부 snake_case인 리포가 63% UpperCamel로 보였습니다. `--include-tests`로 켜면 디렉터리별 식별자 수가 로그에 찍히니 이런 오염이 바로 보입니다.

규칙 파일마다 `scope`가 붙고, 손으로 고칠 수 있습니다. 비어 있으면 리포 전체 적용입니다.

```yaml
# pumpkins/rules/member-prefix-m.yml
id: member-prefix-m
category: member_variable
description: "멤버 변수는 `m` 접두사를 사용한다"
facet: prefix
value: m
coverage: 0.98
occurrences: 1842
confidence: high
scope:
  paths: []                        # 비면 전체
  exclude_paths: ["src/legacy"]    # 여기선 이 규칙을 묻지 않음
  extensions: [".hpp", ".h"]       # 헤더에만 적용
reason: "2026-07 팀 논의에서 승인 — 신규 코드에만 적용"
```

- `exclude_paths`가 `paths`를 이깁니다 — "리포 전체, 단 이 레거시 폴더만 예외"가 제외 한 줄로 끝납니다.
- 패턴은 리포 상대 경로에 **대소문자 구분**으로 매칭됩니다(같은 파일이 어느 OS에서도 같은 결과를 내도록). `*`는 디렉터리 경계를 넘고, 디렉터리를 가리키는 패턴은 그 아래 전체를 덮습니다.
- 지적 코멘트에 적용 범위가 함께 표시돼서, 읽는 사람이 "이 규칙이 여기 적용되는 게 맞나"를 판단할 수 있습니다.

주요 옵션: `--profile concurrency`(체크 프로파일 — 현재는 동시성만, 앞으로 컨벤션 등 추가 예정), `--model`(프로바이더별 기본값 오버라이드), `-v`(디버그 로그).

> 모델은 단계별로 다르게 씁니다 — 리뷰(triage)는 정밀도가 생존이라 강한 모델(anthropic: `claude-opus-4-8` / openai: `gpt-4o`), 컨벤션 학습(`learn`)은 구조화 판정이라 저렴한 쪽(anthropic: `claude-sonnet-5` / openai: `gpt-4o-mini`). 근거는 [설계 문서 §4](docs/convention-detection-design.md).

## 테스트

```bash
pytest
```

## 알려진 한계 (프로토타입)

실제 리포 두 곳(사내 C++ 프로젝트, [fmt PR #4865](https://github.com/fmtlib/fmt/pull/4865))에 돌려 확인한 것들입니다. 리포트가 이제 이 공백을 **숨기지 않고 표시**하지만, 없어진 건 아닙니다.

- **헤더 온리 프로젝트에서 clang-tidy 축이 사실상 무용.** 얕은 모드는 헤더를 단독 분석할 수 없어 TU만 봅니다. 구현이 헤더에 인라인된 프로젝트(헤더가 소스보다 네 배 이상 많은 리포를 측정했습니다)에서는 이 축이 항상 0건입니다. `compile_commands.json`을 만들어 주는 것이 유일한 해법 — 리포트가 이 안내를 출력합니다.
- **비C++ 동반 파일은 안 읽습니다.** 확장자 필터를 통과하지 못한 변경(예: 테스트케이스·스펙 파일)은 어떤 단계도 보지 않습니다. 동작 변경과 스펙·테스트가 한 커밋에 오는 리포에서는 리뷰 가치의 절반이 여기 있습니다.
- **체크 프로파일이 2축(`concurrency`·`portability`)뿐이고, 한 번에 하나만 돕니다.** LLM 프롬프트는 이제 프로파일별로 조립되지만, 두 축 밖의 결함(미사용 파라미터, 형제 파일 비대칭 등)은 해당 프로파일이 없어 나오지 않습니다.
- **컨벤션 축의 구조 규칙은 6종.** 명명·헤더 지시자·계층 방향·반환 타입·멤버 소유권·상속까지 학습·검증·리뷰 검사가 닫혔습니다. 그 밖의 관계형 관행(호출 방향, 상태 전이, 에러 처리 관례 등)은 아직 check가 없어 LLM 판단(참고용)에 머뭅니다.
- **전처리기를 온전히 돌리지는 않습니다.** 리포가 정의한 매크로는 `#define`을 읽어 파일 전체에서 펼치지만,
  함수형 매크로와 조건부 컴파일(`#if`)은 해석하지 않아 못 읽는 문법이 남습니다. 다만 **조용히 넘어가지 않습니다** — 판정 못 한 class 선언 수가 learn 출력과
  `learn-report.yml`에 남고, 0이 아니면 경고합니다. 실패 방향은 안전합니다(규칙이 기각될 뿐 틀린 규칙이
  채택되지는 않음).
- 코드 생성 산출물이 통계를 오염시킬 수 있습니다(플레이스홀더 이름 등) — `--exclude`로 빼세요. 다만 실패 방향은 안전한 쪽입니다: 규칙이 **기각**되어 침묵할 뿐, 틀린 규칙이 채택되지는 않습니다.
- clang-tidy 텍스트 출력 파싱 — `--export-fixes` YAML 전환 예정.
- LLM 호출은 diff 전체를 한 번에 전달 — 대형 diff는 아직 청킹 안 함.

## 다음 단계

실제 리포에 돌려본 결과가 정한 순서입니다 — 근거는 [설계 문서 §5.5·§6](docs/convention-detection-design.md).

**최근 완료:** 프로파일 분리(`concurrency`+`portability`) · 규칙을 리뷰 LLM에 주입(방안 B) · 실제 리포
채점 하네스 · learn **2단 여과**(쪼개짐 추론만 강모델로 자동 승급) · **`--infer`**(틀 없는 AI 규칙 추측) +
**기계 검증**(추측을 레포에 대조·채점) · **구조 규칙 확장** — 계층 방향·소유권·상속 check를 추가해
학습·검증·**리뷰 검사**까지 닫음 · **추론 2단 여과** — 싼 모델이 구조 요약으로 훑고 강한 모델이 지목된
파일만 정독 · **매크로 펼치기 + 파싱 건강상태 측정** — 리포의 `#define`을 읽어 파일 전체에서 펼치고
(파싱 오류 337 → 76, 판정불가 class 선언 25 → 0, 멤버 통계 +35%), 그래도 못 읽은 자리는 세어서 보고.

**다음:**

1. **나머지 facet:other의 리뷰 검사** — `header_directive`(예: `#pragma once`)는 학습·검증은 되지만 리뷰
   적용이 아직 LLM 판단(참고용)이다. 다른 구조 check처럼 리뷰측 결정적 체커를 붙이면 됨.
2. **check 어휘 더 넓히기** — 다음에 만들 것은 추론이 알려준다: 모델이 계속 제안하는데 돌릴 검사가 없는
   종류가 후보다 (`base_class`가 실제로 그렇게 생겼다).
3. **헤더 분석** — 임시 TU로 헤더를 include해 분석. 헤더 온리 프로젝트에서 clang-tidy 축이 영구 0건인 문제.
4. **비C++ 동반 파일** — *"동작이 바뀌었는데 스펙/테스트 파일이 안 바뀌었다"* 가 높은 가치의 지적입니다.
5. **출처 라벨 정정** — `reconcile`이 규칙별 판정 모델을 남기지 않아, 추론 규칙이 실제로는 강모델이 판정했는데
   learn 모델로 기록되는 작은 부정확이 있습니다.
6. 대형 diff 청킹, 결과가 유의미하면 GitHub PR 연동(App/Action).
