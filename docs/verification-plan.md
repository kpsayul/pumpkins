# 검증 계획 — "사람 리뷰어의 지적을 재현할 수 있는가"

이 프로토타입의 존재 이유는 아래 가설을 확인하는 것입니다.

> **가설:** 코드 변경분(git diff)만 보고, 실제 리뷰어가 남겼을 지적 —
> ① **프로젝트 컨벤션 위반**과 ② **기능적 문제** — 을 **쓸 만한 정확도로 재현**할 수 있다.

즉 "사람이 리뷰에서 손으로 짚던 것"을 정답지로 삼고, 도구가 그걸 얼마나 되짚어내는지를 잽니다.
(빌드 여부·정적 분석기 종류는 수단일 뿐, 검증 대상이 아닙니다.)

## 실행 도구

`python verification/run_verification.py` — Track A 주입 검증을 자동화한 드라이버.
키 불필요 단계(주입 recall/precision, CLI 스모크)는 실제 실행하고, 키 필요 단계(learn 품질·모델
비교·리뷰 LLM e2e)는 스켈레톤이라 자동 skip — **키가 없어도 flow 전체가 끝까지 돈다.**
채점표는 `verification/results/`에 누적.

## Track A — 컨벤션 위반 재현 (핵심)

명확한 코딩 컨벤션을 가진 리포에서, 규칙을 어긴 변경을 도구가 짚는지 확인합니다. 정답지는 두 갈래:

- **실제 리뷰 코멘트** — "네이밍 이렇게 바꿔라", "멤버 접두사 붙여라" 류의 리뷰가 달렸던 PR/커밋을 찾아, 그 지적 대상 diff를 입력으로.
- **주입한 위반** — 컨벤션이 뚜렷한 리포에서 일부러 규칙을 어긴 diff를 만들어(예: `m_` 빠뜨리기, 함수명 casing 뒤집기) 도구가 잡는지.

```bash
# 리뷰에서 스타일/네이밍 지적이 오갔던 커밋 후보 찾기
git log --oneline --grep='naming\|convention\|style\|rename\|prefix' -i
# 해당 변경분을 리뷰 대상으로
pumpkins --repo . --base <대상>~1 --out report.md
```

기대: 리포트가 사람이 짚었던 그 규칙 위반을 finding으로 지목해야 함.

## Track B — 기능적 문제 재현

버그(특히 동시성)를 **고친 커밋**을 찾아, 그 *직전* 시점의 버그 있는 diff에 파이프라인을 돌립니다.

```bash
# 1. 버그 수정 커밋 F를 찾는다 (커밋 메시지에서 race/deadlock/mutex/atomic 검색)
git log --oneline --grep='race\|deadlock\|data race\|mutex\|atomic' -i

# 2. F의 부모(버그가 살아있는 시점)로 checkout
git checkout F^

# 3. 버그를 "도입한" 커밋 B를 찾아 (git log -S / git blame), B의 diff를 리뷰 대상으로
pumpkins --repo . --base B^ --out report.md
```

기대: 리포트에 F가 고친 그 버그가 finding으로 나타나야 함.

## 대상 레포 후보

컨벤션이 뚜렷하고(Track A) 리뷰·버그 수정 이력이 풍부한(Track B) 조합으로 고릅니다.

| 후보 | 이유 |
|---|---|
| 1. 스타일 가이드가 명문화된 대형 리포 (예: LLVM, Chromium/Google 스타일 계열) | 컨벤션이 문서로 존재 → Track A 정답지 만들기 쉬움 |
| 2. 스레드풀 / 이벤트루프 네트워크 라이브러리 | 동시성 밀도 높고 수정 이력 풍부 → Track B 첫 타깃 |
| 3. RocksDB / folly 급 산업 리포 | 리뷰 문화가 촘촘 — 실제 리뷰 코멘트 정답지 확보 용이 |

> 후보는 실행 시점에 "정답지(리뷰 코멘트/규칙 위반/버그 수정 커밋)를 5분 안에 3개 이상 찾을 수 있는가"로 최종 선정.
> 레포 자체가 목적이 아니라 **채점 가능한 diff 샘플 확보**가 목적.

## 측정 지표

레포당 샘플 ≥ 5개, Track A·B 섞어서 총 25+ 샘플.

| 지표 | 정의 | 목표(가설 채택선) |
|---|---|---|
| Recall | 사람이 짚었던 지적을 도구가 재현한 비율 | ≥ 50% |
| Precision | 도구 finding 중 사람이 봐도 유효한 비율 | ≥ 60% |
| 노이즈 감소율 | LLM triage가 버린 raw 진단 중 실제 노이즈였던 비율 | ≥ 80% |
| LLM 기여도 | 전체 유효 finding 중 `evidence.detector == llm` 비율 | 기록만 (설계 판단용) |
| **재현성 비율** | `evidence.reproducible`인 finding 비율 — CI 게이트로 쓸 수 있는 몫 | 기록만. 같은 입력 3회 반복해 흔들리는 건수도 함께 측정 |
| **모델 비교 (learn)** | 같은 리포의 컨벤션 추출을 Sonnet/Haiku/Opus로 각각 실행 → 추출 규칙의 정확도 비교 | Sonnet이 Opus 대비 손실 없으면 Sonnet 확정 ([설계 문서 §4](convention-detection-design.md) 모델 전략 검증) |
| 비용/시간 | diff당 토큰 비용, wall time | 기록만 |

기록 방법: 샘플별로 `report.md`와 함께 아래 형식의 채점표를 남김.

```
sample: <repo> / <track A|B> / commit abc123^ (ref: 리뷰코멘트 링크 또는 fix 커밋)
expected: "m_ 접두사 누락" 등 사람이 짚었던 지적
reproduced: yes | no | partial
findings_total: N, valid: N, noise: N
notes: ...
```

## 판정

- **채택**: recall·precision 목표선 도달 → GitHub PR 연동(App/Action) 설계 착수 ("사람 대신 코멘트를 다는" 형태)
- **부분 채택**: 한 축(컨벤션 또는 기능)만 목표 도달 → 그 축을 제품의 첫 시장으로 좁힘
- **기각**: 두 축 모두 미달 → 접근 재설계 (규칙을 사람이 명시해주는 반자동 방식 등 재검토)

## 실행 순서

1. Track B 스레드풀 샘플로 파이프라인 자체 버그 제거 (스모크)
2. Track A로 컨벤션 위반 재현 — 명문 규칙 리포에서 주입 위반부터, 그다음 실제 리뷰 코멘트로
3. 대형 diff에서 한계 확인 (청킹 필요성 판단)
4. 채점표 취합 → 판정

## 픽스처 검증의 한계 (2026-07-27 실측으로 확인)

**자체 픽스처 100%는 제품 품질의 증거가 아니다.** [run_verification.py](../verification/run_verification.py)의
주입 검증이 recall/precision 100%인 상태에서 실제 리포 두 곳에 돌렸더니 학습된 규칙이 양쪽 다 틀렸다
([설계 문서 §5.5](convention-detection-design.md)). 픽스처는 직접 만든 코드에 직접 만든 정답지였고,
정작 실패는 픽스처에 없던 것들 — 밑줄 없는 헝가리안 접두사, 벤더링된 테스트 프레임워크,
템플릿 파라미터, 코드 생성 산출물 — 에서 나왔다.

그래서 이 계획에 다음 항목을 **추가**한다:

| 항목 | 내용 |
|---|---|
| **정답 공개 리포로 learn 채점** | 스타일 가이드가 문서로 존재하는 리포에 `learn`을 돌려 문서와 비교. fmt = 전부 snake_case, googletest = Google 스타일(멤버 트레일링 `_`, 함수 UpperCamel). 사람 판단 없이 채점 가능한 유일한 축이라 우선순위가 높다 |
| **다양성 우선** | 위 대상 후보 표는 "리뷰 이력이 풍부한" 기준이었는데, 실측에서 드러난 실패는 **명명 스타일의 다양성**에서 왔다. 헝가리안(`mFoo`) / 트레일링 언더스코어(`foo_`) / snake_case를 각각 대표하는 리포를 최소 하나씩 넣는다 |
| **벤더 트리·생성 코드가 섞인 리포** | 오염 내구성 테스트. 정답은 "규칙을 안전하게 기각"하는 것 |
| **헤더 온리 리포** | clang-tidy 축이 0건이 되는 조건을 지표에 명시적으로 기록 (fmt·사내 리포에서 재현됨) |

### 구현: 실제 리포 learn 채점 (2026-07-28)

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

이 셋은 실측에서 확인된 세 가지 서로 다른 상황을 대표한다:

- **fmt** — 깨끗한 snake_case. learn이 재현해야 정상(양성 케이스).
- **googletest** — 함수/타입 UpperCamel은 게이트를 넘지만, 문서가 요구하는 멤버 트레일링
  `_`는 공개 struct 멤버가 섞여 관측 ~71%로 게이트에 못 미친다 → learn이 **문서화된
  규칙을 놓치는** 것이 관측된다(recall 손실). 이게 "픽스처 100%인데 실제에서 틀림"의 정체.
- **Catch2** — 관행(m_/lowerCamel/UpperCamel)이 모두 85% 미만 → learn이 **안전하게 기각**하는
  것이 정답. 지저분한 리포에서 오채택을 만들지 않는지 보는 오염 내구성 축.

`run_verification.py`의 [5]/[6]도 스켈레톤에서 구현으로 바꿨다. 다만 픽스처는 식별자가
게이트보다 적어(6/6/2 vs 20), 통계를 분포 보존한 채 게이트 위로 스케일해 LLM의 패턴
식별력을 잰다 — 픽스처의 이 작음 자체가 통제 환경의 한계를 재확인한다. [6]의 모델 tier
비교(설계 §4 haiku/sonnet/opus)는 활성 provider가 anthropic이고 유효한
`ANTHROPIC_API_KEY`가 있을 때만 그 세 tier로 돈다(그 외 provider면 해당 provider의
tier로 대신 비교하고 그 사실을 채점표에 남긴다).
