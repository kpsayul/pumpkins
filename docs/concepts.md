# 컨셉 노트 (미구현)

> 아직 **구현하지 않은** 방향을 적어 두는 곳입니다. 합의된 명세가 아니라 **검토 노트** — 언제
> 손댈지, 손댈지 말지도 미정입니다. 구현되면 해당 항목을 [architecture.md](architecture.md)로
> 옮기고 여기서 지웁니다. "왜 이렇게 됐나"의 과거 기록은 [design-history.md](design-history.md).

---

## 추출기 층 분리 — facet 어휘를 어디에 둘 것인가

**문제.** [extractor.py](../src/pumpkins/conventions/extractor.py)가 성격이 다른 두 일을 한 파일에서 한다.

| 층 | 지금 위치 | 성격 |
|---|---|---|
| ① 언어 문법 — 선언이 어떻게 생겼나 | [cpp/parser.py](../src/pumpkins/languages/cpp/parser.py) | 언어 특화 (분리됨) |
| ② **facet 어휘** — 어떤 접두사/접미사/casing이 있나 (`m_`,`s_`,`g_`,`mFoo`, casing 종류) | **extractor.py** (`split_pattern`, `_classify_casing`) | 언어/컨벤션 특화 (섞임) |
| ③ 통계 기계 — 분포 세기·임계선 게이트·쪼개짐 감지 | extractor.py | 언어 중립 |

`cpp/parser.py`는 "선언이 어떻게 생겼나"(①), `extractor.py`는 "이 이름들의 공통점은?"(③)을 답하도록 나뉘어
있는데, **②(어떤 affix가 컨벤션 후보인가)가 중립이어야 할 ③ 옆에 하드코딩**돼 있다. ②는 사실 ①과 같은
"언어/프로젝트 지식"이다 — 어떤 리포는 `m_`, 어떤 리포는 `mFoo`를 쓴다.

**선택지.**

- **A. ②를 `cpp/parser.py`로 이동.** C++ 지식을 한곳에 모으고 `extractor.py`는 순수 중립 통계만 남긴다.
  가장 작은 정리 — 새 추상화를 만들지 않고 **이미 있는 이음매를 날카롭게** 한다. cpp/parser.py의 기존 철학
  ("언어가 하나일 때 플러그인 인터페이스는 성급하다")과 일치.
- **B. 전용 `cpp_extractor.py`.** C++ facet 분해를 별 파일로. A와 비슷하되 파일이 하나 는다. 두 번째
  언어가 올 때 `<lang>_extractor.py` 패턴이 보이는 게 장점.
- **C. facet 어휘를 데이터 파일로.** 접두사/casing 목록을 선언적 파일(YAML 등)로 빼고 추출기가 읽는다.
  프로젝트마다 다르므로(`mFoo` vs `m_foo`) **진짜 데이터**에 가깝다. 이미 있는 리포별
  `pumpkins/settings.yml`(확장자 오버라이드)의 연장선.

**전처리기 자동생성 아이디어(사용자 제안)와 그 한계.** "언어 스펙 파일을 정하고 거기서 추출기(전처리기)를
자동생성" — 매력적이지만, **이건 이미 tree-sitter가 하는 일이다**(선언적 문법 → 파서). 우리만의 정규식-스펙
포맷 + 생성기를 만드는 건 (a) 유지할 미니 컴파일러를 새로 떠안는 것이고, (b) 정규식 추출은 애초에
tree-sitter/AST로 **대체하려는** 기법이라, 그걸 자동생성하는 건 사라질 것에 투자하는 셈이다.

**잠정 결론 (구현 시).**

1. **②(facet 어휘)를 ③에서 떼라** — ✅ 했다. facet 어휘는 [naming.py](../src/pumpkins/languages/cpp/naming.py)로
   나갔고(A안보다 나은 전용 모듈), `extractor`는 이제 **통계만** 남았다.
2. **facet 어휘는 데이터로 뺄 값어치가 있다**(C) — 아직. `naming.py`에 상수로 하드코딩돼 있다. 리포마다
   다른 값(어떤 팀은 `mFoo`)이라 `settings.yml`류 선언 파일로 외부화하는 건 남은 걸음.
3. **전면 선언적 추출기(전처리기 자동생성)는 따로 만들지 말고 tree-sitter 이행에 흡수하라** — ✅ 그렇게 됐다.
   자체 스펙-포맷을 만들지 않고 tree-sitter를 썼다.

> **진행 (2026-07): 추출기가 AST로 올라갔다.**
> [extractor](../src/pumpkins/conventions/extractor.py)의 스캔이 이제
> [cpp/ast.py](../src/pumpkins/languages/cpp/ast.py)(tree-sitter)로 돈다 — 템플릿·매크로·여러 줄 선언을
> 정규식보다 정확히 읽는다(정규식 [cpp/parser.py](../src/pumpkins/languages/cpp/parser.py)는 네이티브 lib이
> 깨졌을 때의 폴백 + 리뷰 hunk 검사에 남는다). 층도 갈렸다: **①문법**=`cpp_ast`, **②facet 어휘**=`naming.py`,
> **③통계**=`extractor`. 남은 건 위 #2(어휘 데이터화)뿐이다.
