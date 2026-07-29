# 검증 — 방법과 실측 ("사람 리뷰어의 지적을 재현할 수 있는가")

이 프로토타입의 존재 이유는 아래 가설을 확인하는 것입니다.

> **가설:** 코드 변경분(git diff)만 보고, 실제 리뷰어가 남겼을 지적 —
> ① **프로젝트 컨벤션 위반**과 ② **기능적 문제** — 을 **쓸 만한 정확도로 재현**할 수 있다.

즉 "사람이 리뷰에서 손으로 짚던 것"을 정답지로 삼고, 도구가 그걸 얼마나 되짚어내는지를 잽니다.
(빌드 여부·정적 분석기 종류는 수단일 뿐, 검증 대상이 아닙니다.)

## 실행 도구

`python verification/run_verification.py` — 컨벤션 축(①) 주입 검증을 자동화한 드라이버.
키 불필요 단계(주입 recall/precision, CLI 스모크 등)는 키 없이 실제 실행하고, learn 품질([5])·모델
비교([6])는 구현돼 키가 있으면 실행, 리뷰 LLM e2e([7])만 아직 스켈레톤이라 자동 skip — **키가 없어도
flow 전체가 끝까지 돈다.**
채점표는 `verification/results/`에 누적.

## 두 축

**① 컨벤션 위반 재현 (핵심).** 컨벤션이 뚜렷한 리포에서 규칙을 어긴 변경을 도구가 짚는지 잰다. 정답지는
두 갈래 — **주입한 위반**(일부러 `m_`를 빼거나 함수 casing을 뒤집은 diff, 통제 채점)과 **실제 리뷰
코멘트**(사람이 실제로 단 네이밍 지적). 아래 "실측"에서 주입 검증과 공개 리포 채점으로 잰다.

**② 기능적 문제 재현.** 버그(특히 동시성)를 고친 커밋을 찾아, 그 *직전* 버그 있는 diff에 파이프라인을
돌려 그 버그가 finding으로 나오는지 잰다. **아직 체계적 채점은 안 했다** — 프로파일 단위 수동 확인만
(fmt PR의 C++17 `inline` 변수, spdlog의 `std::source_location` 적발). "남은 검증" 참고.

## 측정 지표

지표의 **정의**와 채택 기준. 실제로 실행한 결과는 아래 "실측".

| 지표 | 정의 | 목표(가설 채택선) |
|---|---|---|
| Recall | 사람이 짚었던 지적을 도구가 재현한 비율 | ≥ 50% |
| Precision | 도구 finding 중 사람이 봐도 유효한 비율 | ≥ 60% |
| 노이즈 감소율 | LLM triage가 버린 raw 진단 중 실제 노이즈였던 비율 | ≥ 80% |
| LLM 기여도 | 전체 유효 finding 중 `evidence.detector == llm` 비율 | 기록만 (설계 판단용) |
| **재현성 비율** | `evidence.reproducible`인 finding 비율 — CI 게이트로 쓸 수 있는 몫 | 기록만. 같은 입력 3회 반복해 흔들리는 건수도 함께 측정 |
| **모델 비교 (learn)** | 같은 리포의 컨벤션 추출을 여러 등급으로 실행 → 비교. **두 과제를 따로 잰다**: ① 규칙 판정 정확도 ② 숨은 쪼개짐의 가르는 기준을 알아내는가 | ①은 싼 등급도 통과했고 ②는 실패했다([설계 문서 §4.1](convention-detection-design.md)). 등급별로 두 축을 각각 기록해 2단 여과의 경계를 정한다 |
| 비용/시간 | diff당 토큰 비용, wall time | 기록만 |

## 남은 검증

- **기능 축(②) 체계적 채점** — 버그 수정 커밋을 정답지로 삼아 동시성·이식성 재현율을 잰다(지금은 수동 확인만).
- **실제 리뷰 코멘트를 정답지로** — 사람이 실제로 단 지적을 모아 채점(설계 문서 방안 C). GitHub 연동 선행.
- **다양성·오염 케이스** — 명명 스타일이 다른 리포(`mFoo` / `foo_` / snake_case)와 벤더 트리·헤더 온리 리포까지.

## 픽스처 검증의 한계 (2026-07-27 실측으로 확인)

**자체 픽스처 100%는 제품 품질의 증거가 아니다.** [run_verification.py](../verification/run_verification.py)의
주입 검증이 recall/precision 100%인 상태에서 실제 리포 두 곳에 돌렸더니 학습된 규칙이 양쪽 다 틀렸다
([설계 문서 §5.5](convention-detection-design.md)). 픽스처는 직접 만든 코드에 직접 만든 정답지였고,
정작 실패는 픽스처에 없던 것들 — 밑줄 없는 헝가리안 접두사, 벤더링된 테스트 프레임워크,
템플릿 파라미터, 코드 생성 산출물 — 에서 나왔다.

그래서 **정답이 문서로 존재하는 공개 리포로 `learn`을 채점**했다 — 사람 판단 없이 잴 수 있는 유일한 축이라
우선순위가 높다. (아직 못 채운 다양성·오염·헤더 온리 케이스는 "남은 검증".)

### 실제 리포 learn 채점 (2026-07-28)

위 "정답 공개 리포로 learn 채점"을 [score_real_repos.py](../verification/score_real_repos.py)로
자동화했다. 세 리포를 얕게(depth 1) 작업 디렉터리에 클론하고(pumpkins 리포에 커밋하지 않는다),
각 리포의 **문서화된(또는 de-facto) 스타일을 정답지**로 삼아 `learn`이 채택한 규칙의
정확도를 잰다. 채점표는 `verification/results/real-repos-*.md`.

```bash
python verification/score_real_repos.py                  # 기본 learn 모델로 3개 리포 채점
python verification/score_real_repos.py --compare-models # provider의 모든 tier로 비교(호출 다수)
python verification/score_real_repos.py --clone-dir DIR --keep-clones  # 재클론 방지
```

정답지 근거 (리포 자체 문서에서 인용) 와 대표 스타일:

| 리포 | 정답지 근거 | 정답 규칙 (category/facet=value) |
|---|---|---|
| **fmt** | `CONTRIBUTING.md`: Google C++ Style, 단 함수/타입은 snake_case | function/casing=lower_snake, class_type/casing=lower_snake, member/casing=lower_snake |
| **googletest** | `CONTRIBUTING.md`: Google C++ Style Guide | function/casing=UpperCamel, class_type/casing=UpperCamel, member/suffix=`_` |
| **Catch2** | 명문 네이밍 문서 없음 → de-facto(관측 지배 + 통용) | member/prefix=m_, function/casing=lowerCamel, class_type/casing=UpperCamel |

채점 정의: **recall** = 정답 규칙 중 learn이 채택한 비율, **precision** = 정답이 정의한
축(category,facet)에 올린 규칙 중 값이 맞은 비율(정답지가 다루지 않는 축의 추가 채택은
벌하지 않는다). 채점표는 규칙마다 **관측 커버리지**와 **게이트(occurrences ≥ 20 AND
coverage ≥ 85%) 통과 여부**를 같이 실어, 왜 채택/기각됐는지를 스스로 설명한다.

이 셋은 실측에서 확인된 세 가지 서로 다른 상황을 대표한다 (openai gpt-4o-mini/gpt-4o/gpt-4.1 3개 모델 실행 기준):

- **fmt — recall 100%** (전 모델). 깨끗한 snake_case를 learn이 그대로 재현(양성 케이스).
- **googletest — recall 67%** (전 모델). 함수/타입 UpperCamel은 게이트를 넘지만, 문서가
  요구하는 멤버 트레일링 `_`는 공개 struct 멤버가 섞여 관측 71%로 게이트에 못 미친다 →
  learn이 **문서화된 규칙을 놓치는** 것이 관측된다(recall 손실). 이게 "픽스처 100%인데
  실제에서 틀림"의 정체.
- **Catch2 — recall 0~33%** (실행마다 흔들림). 관행(m_/lowerCamel/UpperCamel)이 모두 85%
  미만 → learn이 **안전하게 기각**하는 것이 정답(오염 내구성 축). 드물게 LLM이 커버리지를
  과대보고해 `m_`를 채택하기도 하는데(관측 60%인데 통과), 이는 게이트가 관측이 아니라
  **LLM이 보고한 coverage를 신뢰**하기 때문 — 채점표의 관측 커버리지 표가 이 괴리를 드러낸다.

**결정적 관찰: 모델 tier를 올려도(gpt-4o-mini → gpt-4.1) 놓친 규칙이 회복되지 않는다.**
googletest 멤버 `_`는 세 모델 모두 놓쳤다 — 병목이 모델 지능이 아니라 **게이트 + 관측
커버리지**임을 뜻한다. learn의 실제 실패는 더 비싼 모델로 사는 게 아니라, 게이트가 무엇을
어떻게 재는지(예: 문서화된 규칙이 공개 struct 멤버로 희석되는 문제)를 고쳐야 사는 것.

`run_verification.py`의 [5]/[6]도 스켈레톤에서 구현으로 바꿨다. 다만 픽스처는 식별자가
게이트보다 적어(6/6/2 vs 20), 통계를 분포 보존한 채 게이트 위로 스케일해 LLM의 패턴
식별력을 잰다 — 픽스처의 이 작음 자체가 통제 환경의 한계를 재확인한다. [5]는 같은 입력을
3회 반복해 재현성(flap)까지 잰다(측정지표 표의 재현성 항목). [6]의 모델 비교는 **활성
provider의 3 tier**로 돈다 — anthropic이면 설계 §4의 원안인 `haiku/sonnet/opus`, openai면
그에 대응하는 `gpt-4o-mini/gpt-4o/gpt-4.1`. 토큰/비용은 채점표에 기록하며, 사용량은
`ConventionLearner.learn_with_usage()` 공개 API로 얻는다.
