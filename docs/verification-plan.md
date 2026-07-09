# 검증 계획 — "빌드 없이 diff만 보고 잡을 수 있는가"

이 프로토타입의 존재 이유는 아래 가설을 확인하는 것입니다.

> **가설:** compile_commands.json 없이(얕은 모드 포함) git diff 범위의 clang-tidy + LLM 후처리만으로,
> 실제 프로젝트의 동시성 버그를 **쓸 만한 노이즈율로** 잡아낼 수 있다.

## 방법: 버그 수정 커밋 역추적

실제 오픈소스 레포에서 **동시성 버그를 고친 커밋**을 찾아, 그 *직전* 시점으로 되감고
버그가 들어있던 diff에 파이프라인을 돌립니다.

```bash
# 1. 버그 수정 커밋 F를 찾는다 (커밋 메시지에서 race/deadlock/mutex/atomic 검색)
git log --oneline --grep='race\|deadlock\|data race\|mutex\|atomic' -i

# 2. F의 부모(버그가 살아있는 시점)로 checkout
git checkout F^

# 3. 버그를 "도입한" 커밋 B를 찾아 (git log -S / git blame), B의 diff를 리뷰 대상으로
cpp-review --repo . --base B^ --out report.md
```

기대: 리포트에 F가 고친 그 버그가 finding으로 나타나야 함.

## 대상 레포 후보 (5곳)

동시성 코드가 많고, 버그 수정 이력이 풍부하고, 크기가 다양한 조합:

| 후보 | 이유 | compile DB |
|---|---|---|
| 1. 스레드풀 라이브러리 (예: progschj/ThreadPool 계열 fork들) | 작고 동시성 밀도 최고 — 파이프라인 디버깅용 첫 타깃 | 없음 → 얕은 모드 검증 |
| 2. muduo / libuv 스타일 이벤트루프 네트워크 라이브러리 | condvar·lock 패턴 다수, 수정 이력 풍부 | CMake로 생성 가능 → 두 모드 비교 |
| 3. RocksDB | 대형·산업급, 동시성 버그 수정 커밋 검색이 쉬움 | 생성 가능 |
| 4. Redis (C지만 C++ 파서 관용 확인) 또는 folly | 대규모 diff에서의 스케일 확인 | folly는 생성 가능 |
| 5. 중형 게임엔진/DB 커넥터류 (헤더 온리 포함) | 헤더 변경 중심 diff에서 LLM-only 커버리지 확인 | 없음 |

> 후보는 실행 시점에 "동시성 수정 커밋을 5분 안에 3개 이상 찾을 수 있는가"로 최종 선정.
> 못 찾으면 교체 — 레포 자체가 목적이 아니라 diff 샘플 확보가 목적.

## 측정 지표

레포당 diff 샘플 ≥ 5개 (버그 있는 diff 3 + 무해한 diff 2 섞기), 총 25+ 샘플.

| 지표 | 정의 | 목표(가설 채택선) |
|---|---|---|
| Recall | 알려진 버그가 finding으로 잡힌 비율 | ≥ 50% (얕은 모드 ≥ 30%) |
| Precision | finding 중 사람이 봐도 유효한 비율 | ≥ 60% |
| 노이즈 감소율 | LLM triage가 버린 raw 진단 중 실제 노이즈였던 비율 | ≥ 80% |
| LLM 기여도 | 전체 유효 finding 중 `source: llm`(extra_findings) 비율 | 기록만 (설계 판단용) |
| 비용/시간 | diff당 토큰 비용, wall time | 기록만 |

기록 방법: 샘플별로 `report.md`와 함께 아래 형식의 채점표를 남김.

```
sample: rocksdb / commit abc123^ (fix: def456)
known_bug_found: yes | no | partial
findings_total: N, valid: N, noise: N
notes: ...
```

## 판정

- **채택**: recall·precision 목표선 도달 → GitHub PR 연동(App/Action) 설계 착수
- **부분 채택**: compile-DB 모드만 목표 도달 → "compile DB 필수" 제품 방향으로 전환
- **기각**: 얕은/DB 모드 모두 미달 → libclang AST 기반 접근 또는 LLM-only 접근 재검토

## 실행 순서

1. 후보 1(스레드풀)로 파이프라인 자체 버그 제거 (스모크)
2. 후보 2~3에서 compile-DB vs 얕은 모드 **동일 diff 비교** — 얕은 모드의 실질 손실 측정
3. 후보 4에서 대형 diff 한계 확인 (청킹 필요성 판단)
4. 후보 5에서 헤더 중심 diff의 LLM 커버리지 확인
5. 채점표 취합 → 판정
