# LLM 프로바이더 & API 키

> 현재 동작을 요약한 레퍼런스입니다. 코드: [llm/provider.py](../src/pumpkins/llm/provider.py)(어댑터),
> [config.py](../src/pumpkins/config.py)(프로바이더·모델·키 매핑). 전체 구조는 [architecture.md](architecture.md).

## 프로바이더 선택

- 환경변수 **`LLM_PROVIDER`** 로 런타임에 고른다: `anthropic`(기본) 또는 `openai`. 값이 잘못되면 즉시 종료(exit 2).
- 리뷰(triage)와 learn이 공유하는 어댑터 `get_client()`가 프로바이더별 클라이언트를 돌려주고, 두 호출부는
  `.parse(system, user, schema, …) → (파싱된 객체, 토큰수)` 한 인터페이스만 쓴다. 프롬프트·Pydantic 스키마·게이트
  같은 핵심 로직은 프로바이더와 무관하다.

## 두 SDK의 차이 (어댑터가 흡수한다)

| 항목 | Anthropic | OpenAI |
|---|---|---|
| 패키지 | `anthropic` | `openai` |
| 키 환경변수 | `ANTHROPIC_API_KEY` | `OPENAI_API_KEY` |
| 클라이언트 | `anthropic.Anthropic()` | `openai.OpenAI()` |
| 구조화 호출 | `client.messages.parse(...)` | `client.beta.chat.completions.parse(...)` |
| system 프롬프트 | `system=` 별도 인자 | `messages`에 `{"role":"system"}` |
| 파싱 결과 | `response.parsed_output` | `response.choices[0].message.parsed` |
| 토큰 사용량 | `usage.input_tokens` / `output_tokens` | `usage.prompt_tokens` / `completion_tokens` |

## 모델

- 단계·프로바이더별 기본 모델은 `config.PROVIDER_MODELS`에서 파생한다 — `default_review_model()` /
  `default_learn_model()`. anthropic이면 리뷰 `claude-opus-4-8` / learn `claude-sonnet-5`, openai면
  `gpt-4o` / `gpt-4o-mini`. 단계별 근거는 [설계 문서 §4](convention-detection-design.md).
- CLI `--model`이 항상 우선한다.

## API 키

- **키는 코드·커밋에 절대 없다.** 각 SDK가 환경변수에서 직접 읽는다(`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`).
- **`.env` 자동 로드**: CLI 진입 시 현재 디렉터리의 `.env`를 `python-dotenv`로 읽되, 이미 설정된 실제
  환경변수가 우선한다(`override=False` — CI·컨테이너의 셸 주입과 충돌 없음). 저장소엔 `.env.example` 템플릿만
  커밋하고 `.env`는 `.gitignore`에 있다.
- 선택한 프로바이더의 키가 없으면: 리뷰는 경고 후 `--no-llm` 폴백, learn은 에러 + `--no-llm`(통계 덤프) 안내.

## 구조화 출력 주의점

OpenAI structured output은 일부 모델·중첩 스키마에서 제약이 있을 수 있다. 파싱 결과가 `None`이면 조용히
넘기지 않고 `RuntimeError`를 낸다 — 미검증 결과를 내지 않기 위해서. (실측 2026-07-19: 현재 스키마
`_LlmReview`/`LearnResult`는 두 프로바이더에서 통과.)
