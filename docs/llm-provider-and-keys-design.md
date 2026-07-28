# LLM 프로바이더 전환 & API 키 관리 설계

> 상태: **구현됨** (2026-07-19). 결제 이슈로 Claude 대신 GPT를 쓸 수 있게, 그리고
> 사람마다 자기 키를 안전하게 주입할 수 있게 하기 위한 설계. 어댑터는
> `src/pumpkins/llm/provider.py`, 프로바이더/모델/키 매핑은 `config.py`에 있다.
>
> 이건 **설계 기록**이다 — §1~3은 구현 *전* 제안(근거 보존용, "미구현" 표시가 남아 있음),
> §4·§6이 실제로 만들어진 결과다. **현재 동작의 기준은 §6과 코드**이며, 현재 구조 요약은
> [architecture.md](architecture.md).

## 1. 배경

- 현재 LLM 호출은 **Anthropic SDK로 하드코딩**돼 있음 — 2군데:
  - [`llm/postprocess.py`](../src/pumpkins/llm/postprocess.py) — Stage 3 진단 triage
  - [`conventions/learner.py`](../src/pumpkins/conventions/learner.py) — Learn L2 규칙 판정
- 두 곳 다 `anthropic.Anthropic()` + `client.messages.parse(system=…, messages=…, output_format=PydanticModel)` 패턴.
- 키는 코드·설정에 두지 않고 `ANTHROPIC_API_KEY` 환경변수에서만 읽는 설계
  ([config.py](../src/pumpkins/config.py), design-history.md §Stage 3). 이 원칙은 유지한다.
- (설계 당시) `.env`·`.envrc`는 `.gitignore`에 등록돼 있었지만 **자동 로드 코드가 없어**
  셸 `export`로만 동작했다 — 이 설계로 `python-dotenv` 자동 로드가 추가됐다(§4·§6).

## 2. 목표

1. `LLM_PROVIDER` 값에 따라 Claude / GPT 경로를 **런타임에 갈라타기**.
2. 사람마다 `.env`에 자기 키를 넣고, 저장소엔 `.env.example` 템플릿만 커밋.
3. 기존 원칙 유지: **키는 절대 코드·커밋에 안 들어감**, 구조화 출력(Pydantic) 유지,
   `--no-llm` 폴백·명시적 파싱 에러 처리 유지.

## 3. 프로바이더 추상화

두 SDK의 호출 형태 차이:

| 항목 | Anthropic | OpenAI |
|---|---|---|
| 패키지 | `anthropic` | `openai` |
| 키 환경변수 | `ANTHROPIC_API_KEY` | `OPENAI_API_KEY` |
| 클라이언트 | `anthropic.Anthropic()` | `openai.OpenAI()` |
| 구조화 호출 | `client.messages.parse(...)` | `client.beta.chat.completions.parse(...)` |
| system 프롬프트 | `system=` 별도 인자 | `messages`에 `{"role":"system"}` |
| 파싱 결과 | `response.parsed_output` | `response.choices[0].message.parsed` |
| 토큰 사용량 | `usage.input_tokens` / `output_tokens` | `usage.prompt_tokens` / `completion_tokens` |
| 기본 모델 | `claude-opus-4-8`(review) / `claude-sonnet-5`(learn) | 예: `gpt-4o`(review) / `gpt-4o-mini`(learn) |

### 제안: 얇은 어댑터 하나

`postprocess.py`와 `learner.py`가 공유하는 최소 인터페이스를 새 모듈
(`llm/provider.py` 정도)로 뽑는다. 프롬프트·Pydantic 모델·threshold gate 등
**핵심 로직은 그대로 재사용**되고, 바뀌는 건 "system+messages+출력스키마 → 파싱된 객체+토큰수"
한 호출뿐이다.

```
# 개념 스케치 (미구현)
class LlmClient(Protocol):
    def parse(self, *, model, system, user, schema) -> ParsedResult: ...
        # ParsedResult = (parsed: BaseModel|None, in_tokens: int, out_tokens: int)

def get_client(provider: str | None = None) -> LlmClient:
    provider = provider or os.environ.get("LLM_PROVIDER", "anthropic")
    return {"anthropic": AnthropicClient, "openai": OpenAIClient}[provider]()
```

- `postprocess.LlmPostProcessor` / `conventions.ConventionLearner`는
  `anthropic.Anthropic()` 직접 생성 대신 `get_client()`를 받아 `.parse(...)` 호출.
- 반환을 `(parsed, in_tokens, out_tokens)`로 통일하면 기존 로그 라인
  (`tokens in=%d out=%d`)이 프로바이더 무관하게 그대로 유지됨.

### 모델 선택

- config.py의 `DEFAULT_REVIEW_MODEL` / `DEFAULT_LEARN_MODEL`을 프로바이더별로 분리.
  예: `MODELS = {"anthropic": ("claude-opus-4-8", "claude-sonnet-5"),
  "openai": ("gpt-4o", "gpt-4o-mini")}` — 기본 모델을 프로바이더 선택에서 파생.
- CLI `--model` 오버라이드는 지금처럼 그대로 우선.

### 구조화 출력 주의점

- OpenAI structured output(`beta.chat.completions.parse` + `response_format=Pydantic`)은
  일부 모델·중첩 스키마에서 제약이 있음. 현재 스키마(`_LlmReview`, `LearnResult`)의
  중첩 리스트가 문제되면 JSON 모드 + 수동 파싱으로 폴백하는 경로를 남겨둔다.
- `parsed`가 None인 경우 기존처럼 `RuntimeError` — 조용히 미검증 결과 내지 않기.

## 4. 키·설정 파일 관리

### 채택안: `.env` + `.env.example` + dotenv 자동 로드

1. **`.env.example`** (커밋됨) — 플레이스홀더 템플릿:
   ```dotenv
   # 사용할 프로바이더: anthropic | openai
   LLM_PROVIDER=anthropic

   # 쓰는 쪽 키만 채우면 됨
   ANTHROPIC_API_KEY=sk-ant-...
   OPENAI_API_KEY=sk-...
   ```
2. **`.env`** (gitignore됨, 이미 등록) — 각자 복사해서 실제 키 기입.
   `cp .env.example .env` 후 편집.
3. **자동 로드** — `python-dotenv`를 dev 아닌 런타임 의존성에 추가하고,
   CLI 진입점([cli.py](../src/pumpkins/cli.py) `main` 초입)에서 `load_dotenv()` 1회 호출.
   - `override=False`(기본)로 두어 **이미 설정된 실제 환경변수가 우선**하게 함
     (CI·컨테이너에서 셸 주입 방식과 충돌 안 남).
   - 키 자체는 여전히 SDK가 환경변수에서 읽음 — "코드에 키 없음" 원칙 유지.

### 대안 (참고, 미채택)

- **dotenv 없이 `.env.example`만**: 문서로 `export` 안내. 의존성 0이지만 사람마다
  셸 설정을 해야 해서 "파일 하나 채우면 끝"의 편의가 없음.
- **`config.toml` 등 별도 설정 파일**: 키까지 담으면 실수로 커밋 위험 ↑.
  env 원칙과 어긋나 비채택.

## 5. 폴백·에러 처리 (기존 원칙 유지, 프로바이더 일반화)

- 현재 `ANTHROPIC_API_KEY` 하드코딩 체크가 있는 지점
  ([cli.py:83](../src/pumpkins/cli.py#L83), [cli.py:186](../src/pumpkins/cli.py#L186),
  [verification/run_verification.py](../verification/run_verification.py))를
  **선택된 프로바이더의 키 유무 체크**로 일반화:
  `required_key = {"anthropic":"ANTHROPIC_API_KEY","openai":"OPENAI_API_KEY"}[provider]`.
- 키 없으면 지금처럼 경고 후 `--no-llm` 폴백(리뷰) / 에러+안내(learn).

## 6. 변경 대상 요약 (구현 시 체크리스트)

- [x] `pyproject.toml` — `openai`, `python-dotenv` 의존성 추가 (anthropic는 유지).
- [x] `llm/provider.py` (신규) — 어댑터 + `get_client()`.
- [x] `llm/postprocess.py` — 클라이언트 직접 생성 → `get_client()` 사용.
- [x] `conventions/learner.py` — 동일.
- [x] `config.py` — 프로바이더별 모델 매핑, 기본 모델 파생 (`current_provider()` /
      `default_review_model()` / `default_learn_model()` / `has_api_key()`).
- [x] `cli.py` — `load_dotenv()` 호출 + 키 체크 프로바이더 일반화 + 잘못된
      `LLM_PROVIDER`는 exit 2로 즉시 에러.
- [x] `.env.example` (신규 커밋).
- [x] `verification/run_verification.py` — 키 체크 일반화 + `.env` 로드.
- [x] README §설정 — `.env` 복사 + `LLM_PROVIDER` 안내로 갱신.
- [x] design-history.md §Stage 3 / 실패처리 표 — 프로바이더 일반화 반영.
- [x] 실키 테스트 (2026-07-19) — OpenAI 구조화 출력이 중첩 스키마
      (`_LlmReview`/`LearnResult`, `Field(ge/le)` 제약 포함)를 통과함을 확인.
      fixture 대상 `pumpkins learn` e2e도 정상 (gpt-4o-mini, in=1012/out=232 토큰;
      임계선 게이트가 소규모 fixture의 규칙을 설계대로 전부 강등). JSON 모드
      폴백(§3 주의점)은 필요 없어짐.
