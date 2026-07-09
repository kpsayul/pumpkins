# cpp-review-bot

> ⚠️ **검증용 프로토타입입니다.** 제품이 아니라 *"빌드 없이 git diff만 보고 위험한 C++ 패턴을 잡을 수 있는가"* 를 확인하기 위한 실험용 파이프라인입니다. 웹서버·GitHub App 없음, CLI만 있습니다.

git diff → clang-tidy 정적 분석 → LLM(Claude) 후처리(노이즈 제거·심각도 판정·설명/수정안 생성) → 마크다운 리포트.

## 핵심 제약

- **대상 프로젝트를 절대 빌드하지 않습니다.** `compile_commands.json`이 있으면 활용하고(`-p`), 없으면 **얕은 모드**(추정 플래그 `-std=c++17 -I...`)로 동작합니다. 얕은 모드의 진단은 신뢰도가 낮으며 리포트에 명시됩니다.
- **변경된 라인 주변만 분석합니다.** clang-tidy `--line-filter`로 diff 라인 범위 ±15줄만 대상.
- **초점 버그 클래스: 동시성 안티패턴.** lock 순서 역전, 공유 멤버 무보호 접근, volatile 오용 등. clang-tidy가 못 잡는 패턴(lock 순서 등)은 LLM이 diff에서 직접 탐지합니다. 체크 프로파일은 `analysis/checks.py`에서 확장 가능.

## 문서

| 문서 | 내용 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 파이프라인 단계별 상세 설계, 얕은 모드 처리 규칙, 실패 처리 원칙 |
| [docs/verification-plan.md](docs/verification-plan.md) | 오픈소스 5개 레포 검증 방법(버그 수정 커밋 역추적), 측정 지표, 채택/기각 기준 |
| [docs/extending.md](docs/extending.md) | 체크 프로파일·diff 소스·분석기·리포트 포맷 확장 방법 |

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
| `llm/postprocess.py` | Claude 구조화 출력으로 노이즈 필터 + 심각도 + 설명/수정안 + 추가 탐지 |
| `report/markdown.py` | 마크다운 리포트 렌더링 |
| `models.py` | 단계 간 데이터 계약 (`DiffScope`, `RawDiagnostic`, `Finding`, `ReviewResult`) |

## 설치

요구사항: Python ≥ 3.10, `clang-tidy` (PATH에 있어야 함).

```bash
# 가상환경
python3 -m venv .venv
source .venv/bin/activate

# 설치 (개발용)
pip install -e ".[dev]"

# API 키 (코드에 하드코딩하지 않음 — 환경변수로만)
export ANTHROPIC_API_KEY=sk-ant-...
```

## 실행

```bash
# 대상 레포의 워킹트리 변경분(uncommitted) 리뷰
cpp-review --repo /path/to/cpp/project

# PR처럼: main 기준 브랜치 변경분 리뷰, 파일로 출력
cpp-review --repo /path/to/cpp/project --base main --out report.md

# LLM 없이 clang-tidy 원본 결과만 (파이프라인 디버깅용)
cpp-review --repo /path/to/cpp/project --no-llm -v
```

주요 옵션: `--profile concurrency`(체크 프로파일), `--model`(기본 `claude-opus-4-8`), `-v`(디버그 로그).

## 테스트

```bash
pytest
```

## 알려진 한계 (프로토타입)

- 얕은 모드에서는 헤더 단독 분석 불가(TU만 분석), 컴파일 에러성 진단은 버림.
- clang-tidy 텍스트 출력 파싱 — `--export-fixes` YAML 전환 예정.
- LLM 호출은 diff 전체를 한 번에 전달 — 대형 diff는 아직 청킹 안 함.

## 다음 단계

1. **오픈소스 레포 5곳에서 검증 실행** — 동시성 버그 이력이 있는 프로젝트(예: 스레드풀/서버류) 골라, 버그 수정 커밋 *직전* 시점을 checkout하고 해당 diff에 파이프라인을 돌려 탐지율/노이즈율 측정.
2. 측정 지표 정리: precision(노이즈율), 알려진 버그 recall, LLM 필터 전후 비교.
3. `--export-fixes` 기반 구조화 파싱 + note 진단(연관 위치) 활용.
4. 대형 diff 청킹 및 파일별 LLM 호출 병렬화.
5. 결과가 유의미하면 그때 GitHub PR 연동(App/Action) 검토.
