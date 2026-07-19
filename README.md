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

**② 컨벤션 학습 + 지적 (MVP 1·2단계)** — `pumpkins learn` → 리뷰에 자동 연결

리포 스캔 → 식별자 명명 통계(기계적 추출 — LLM엔 통계 요약만 전달) → LLM 규칙 판정 → 임계선 게이트(20회+/85%+) → 사람이 검수·커밋하는 **`conventions.yml`** 생성.

이후 리뷰(`pumpkins`) 실행 시 `<repo>/conventions.yml`이 있으면 diff를 규칙과 대조해 **질문형으로 지적**합니다 — *"`running` — 멤버 변수는 `m_` 접두사를 사용한다 관행과 다른 것 같아요. 여기만 다르게 한 이유가 있을까요?"* (근거 수치 + rename 제안 포함). 이 대조는 결정적이라 **API 키 없이도 동작**합니다.

## 문서

| 문서 | 내용 |
|---|---|
| [docs/convention-detection-design.md](docs/convention-detection-design.md) | **핵심 기능 설계 검토** — 사람 리뷰어의 지적을 자동화하는 방안(A/B/C), 설계 결정, 리스크, MVP 경로 |
| [docs/architecture.md](docs/architecture.md) | 현재 파이프라인 단계별 상세 설계, 분석 모드, 실패 처리 원칙 |
| [docs/verification-plan.md](docs/verification-plan.md) | "리뷰어의 판단을 재현할 수 있는가" 검증 방법, 측정 지표, 채택/기각 기준 |
| [docs/extending.md](docs/extending.md) | 규칙(체크) 프로파일·diff 소스·분석기·리포트 포맷 확장 방법 |
| [docs/llm-provider-and-keys-design.md](docs/llm-provider-and-keys-design.md) | LLM 프로바이더(Claude/GPT) 전환 & API 키 관리 설계 |

## 아키텍처

```
git diff ──▶ DiffScope ──▶ RawDiagnostic[] ──▶ Finding[] ──▶ report.md
   diff/collector    analysis/clang_tidy   llm/postprocess   report/markdown
```

| 모듈 | 역할 |
|---|---|
| `diff/collector.py` | `git diff` 수집·파싱 → 파일별 변경 라인 범위 + hunk 문맥 |
| `analysis/clang_tidy.py` | clang-tidy를 별도 프로세스로 실행, 진단 파싱 (compile-DB / 얕은 모드) |
| `analysis/checks.py` | 체크 프로파일 (현재 `concurrency`, 확장 가능) |
| `llm/provider.py` | LLM 프로바이더 어댑터 — `LLM_PROVIDER`(anthropic/openai)에 따라 구조화 출력 클라이언트 선택 |
| `llm/postprocess.py` | LLM 구조화 출력으로 노이즈 필터 + 심각도 + 설명/수정안 + 추가 탐지 |
| `report/markdown.py` | 마크다운 리포트 렌더링 |
| `models.py` | 단계 간 데이터 계약 (`DiffScope`, `RawDiagnostic`, `Finding`, `ReviewResult`) |
| `conventions/extractor.py` | (learn L1) 정규식 기반 식별자 추출 → 명명 통계 |
| `conventions/learner.py` | (learn L2) LLM 규칙 판정 + 임계선 게이트 + `conventions.yml` 렌더 |
| `conventions/checker.py` | (리뷰 3.5단계) diff를 `conventions.yml`과 결정적 대조 → 질문형 finding |

## 빠른 시작

처음이라면 아래 순서를 그대로 따라 하면 됩니다.

### 0. 요구사항 확인

- **Python ≥ 3.10** — `python3 --version`
- **clang-tidy** (PATH에 있어야 함) — `clang-tidy --version`
  - macOS: `brew install llvm` 후 PATH에 추가, Ubuntu/Debian: `sudo apt install clang-tidy`

가상환경 생성 방법은 2단계에서 환경에 맞게 고르면 됩니다.

### 1. 저장소 클론

```bash
git clone <이 저장소 URL> pumpkins
cd pumpkins
```

### 2. 가상환경(venv) 생성 및 활성화

프로젝트 전용 파이썬 환경을 만들어 의존성을 시스템과 격리합니다. 아래 **A / B 중 하나**로 `.venv/` 폴더를 만드세요.

**방법 A — 표준 `venv`** (macOS, 대부분의 Linux)

```bash
python3 -m venv .venv
```

> Debian/Ubuntu·WSL에서는 `venv`가 별도 패키지라 이 명령이 실패할 수 있습니다. 그럴 땐 `sudo apt install python3-venv` 후 다시 실행하거나, 아래 방법 B를 쓰세요.

**방법 B — `virtualenv`** (sudo 없이, WSL/Ubuntu에서 검증된 방법)

```bash
pip install --user virtualenv   # 한 번만
virtualenv .venv
```

**생성한 뒤 활성화** (A/B 공통)

```bash
# macOS / Linux
source .venv/bin/activate
# Windows (PowerShell)
#   .venv\Scripts\Activate.ps1

# 활성화되면 프롬프트 앞에 (.venv) 가 붙습니다.
# 끝낼 때는 아무 데서나: deactivate
```

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
cp .env.example .env
# .env를 열어 LLM_PROVIDER와 쓰는 쪽 키를 채우세요:
#   LLM_PROVIDER=anthropic   (또는 openai)
#   ANTHROPIC_API_KEY=sk-ant-...
#   OPENAI_API_KEY=sk-...
```

`.env`는 `.gitignore`에 등록돼 있어 커밋되지 않습니다 — 사람마다 각자 만듭니다. CLI가 실행 시 현재 디렉터리의 `.env`를 자동으로 읽습니다.

셸에서 직접 export해도 됩니다 (이 값이 `.env`보다 **우선**합니다 — CI나 일시적 전환에 유용):

```bash
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-...
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
```

컨벤션 학습 → 지적:

```bash
# 1) 리포의 명명 관행을 학습해 conventions.yml 생성 (검수 후 커밋)
pumpkins learn --repo /path/to/cpp/project

# 2) 이후의 리뷰는 conventions.yml을 자동으로 대조 (API 키 없이도 이 단계는 동작)
pumpkins --repo /path/to/cpp/project --no-llm

# LLM 없이, LLM에 전달될 통계 원본만 출력 (learn 디버깅용)
pumpkins learn --repo /path/to/cpp/project --no-llm
```

컨벤션 관련 옵션: `--conventions PATH`(기본: `<repo>/conventions.yml`), `--no-conventions`(대조 끄기).

주요 옵션: `--profile concurrency`(체크 프로파일 — 현재는 동시성만, 앞으로 컨벤션 등 추가 예정), `--model`(프로바이더별 기본값 오버라이드), `-v`(디버그 로그).

> 모델은 단계별로 다르게 씁니다 — 리뷰(triage)는 정밀도가 생존이라 강한 모델(anthropic: `claude-opus-4-8` / openai: `gpt-4o`), 컨벤션 학습(`learn`)은 구조화 판정이라 저렴한 쪽(anthropic: `claude-sonnet-5` / openai: `gpt-4o-mini`). 근거는 [설계 문서 §4](docs/convention-detection-design.md).

## 테스트

```bash
pytest
```

## 알려진 한계 (프로토타입)

- 얕은 모드에서는 헤더 단독 분석 불가(TU만 분석), 컴파일 에러성 진단은 버림.
- clang-tidy 텍스트 출력 파싱 — `--export-fixes` YAML 전환 예정.
- LLM 호출은 diff 전체를 한 번에 전달 — 대형 diff는 아직 청킹 안 함.

## 다음 단계

1. **컨벤션 지적을 리뷰에 연결 (핵심 차별화).** 학습(`pumpkins learn` — ✅ 구현됨)으로 만든 `conventions.yml`을 리뷰 파이프라인에 물려, diff가 관행을 어기면 질문형으로 지적. 구현 방향은 [docs/convention-detection-design.md](docs/convention-detection-design.md)에 확정.
2. **규칙 프로파일에 컨벤션/스타일 카테고리 추가** — 현재 `concurrency`뿐인 `CHECK_PROFILES`를 규칙 종류별로 확장.
3. **검증** — 실제 리뷰 코멘트·컨벤션 위반이 남아있는 PR을 샘플로, 사람이 짚었던 걸 도구가 얼마나 재현하는지(recall/precision) 측정.
4. 대형 diff 청킹 및 파일별 LLM 호출 병렬화.
5. 결과가 유의미하면 그때 GitHub PR 연동(App/Action) 검토 — "사람 대신 코멘트를 다는" 형태.
