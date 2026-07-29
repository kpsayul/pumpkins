"""Run artifacts — everything a run produced, written to one directory.

    out/
    ├── report.md          렌더된 리포트 (stdout과 동일)
    ├── run.json           실행 출처: 프로바이더·모델·도구 버전·규칙 지문·커버리지
    ├── diff.patch         수집된 diff 원본 (파이프라인이 실제로 본 것)
    ├── diagnostics.json   clang-tidy 원본 진단 (triage 이전)
    ├── findings.json      최종 finding, 구조화
    └── llm/
        ├── request.txt    LLM에 보낸 프롬프트 전문
        └── response.json  LLM이 돌려준 구조화 출력

`run.json`은 편의 기능이 아니라 제품 주장의 근거다. "어떤 모델에서도 재현
가능하고 근거 있는 리뷰"라고 말하려면 무엇으로 돌렸는지가 결과와 함께 남아야
한다 — 모델을 바꿨을 때 차이가 모델 때문인지 규칙 때문인지 도구 버전 때문인지
가릴 수 있어야 하기 때문이다. `rules_fingerprint`가 그 중 규칙 축을 고정한다.

`llm/request.txt`는 디버깅 가치가 가장 크다. "왜 이런 지적을 했지" 또는 "왜
아무 말도 안 하지"의 답이 대개 프롬프트에 그대로 적혀 있다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pumpkins import __version__
from pumpkins.languages.cpp import ast as cpp_ast
from pumpkins.models import DiffScope, RawDiagnostic, ReviewResult

log = logging.getLogger(__name__)

# Files this module owns. Cleared before a write so a later run's artifacts are
# never mixed with an earlier one's — stale files are worse than missing ones
# when you are debugging.
_ARTIFACTS = (
    "report.md",
    "run.json",
    "diff.patch",
    "diagnostics.json",
    "findings.json",
    "llm/request.txt",
    "llm/response.json",
)


@dataclass
class RunContext:
    """Raw inputs a run saw, carried out of the pipeline for dumping only.

    Deliberately not part of ReviewResult: that model is the report's contract,
    and prompts/patches are debugging material, not review output.
    """

    scope: DiffScope | None = None
    diagnostics: list[RawDiagnostic] = field(default_factory=list)
    conventions_path: Path | None = None
    tool_version: str | None = None
    llm_request: str | None = None
    llm_response: dict | None = None


def rules_fingerprint(path: Path | None) -> str | None:
    """Content hash of the rules a run enforced.

    A git SHA would only exist once the rules are committed; a content hash
    always does, and it answers the question that matters when comparing two
    runs — were the rules identical?
    """
    if path is None or not path.exists():
        return None
    digest = hashlib.sha256()
    files = sorted((path / "rules").glob("*.yml")) if path.is_dir() else [path]
    for file in files:
        digest.update(file.name.encode())
        digest.update(file.read_bytes())
    return f"sha256:{digest.hexdigest()[:16]}"


def _git_commit(repo: Path) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True,
    )
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def dump_run(
    out_dir: Path,
    repo: Path,
    result: ReviewResult,
    report_text: str,
    context: RunContext,
) -> list[Path]:
    """Write the run's artifacts; returns the paths written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in _ARTIFACTS:
        (out_dir / name).unlink(missing_ok=True)

    written: list[Path] = []

    def write(name: str, text: str) -> None:
        path = out_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)

    write("report.md", report_text)
    write(
        "run.json",
        json.dumps(
            {
                "pumpkins_version": __version__,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "repo": str(repo),
                "repo_commit": _git_commit(repo),
                "base_ref": result.base_ref,
                "profile": result.profile,
                "analysis_mode": "shallow" if result.shallow_mode else "compile-db",
                "clang_tidy_version": context.tool_version,
                # Which scanner read the code. The regex fallback reads
                # templates and macros differently, so two runs that differ
                # here are not comparable — same reason model/temperature are
                # recorded.
                "ast_engine": cpp_ast.engine(),
                "llm": {
                    "used": result.llm_used,
                    "provider": result.provider,
                    "model": result.model,
                    "temperature": result.temperature,
                },
                "conventions": {
                    "path": str(context.conventions_path) if context.conventions_path else None,
                    "active_rules": result.conventions_loaded,
                    "pending_candidates": result.conventions_pending,
                    "rules_fingerprint": rules_fingerprint(context.conventions_path),
                },
                "coverage": {
                    "analyzed_files": result.analyzed_files,
                    "skipped_headers": result.skipped_headers,
                    "skipped_non_cpp": result.skipped_non_cpp,
                    "complete": not result.has_coverage_gap,
                },
                "counts": {
                    "raw_diagnostics": result.total_diagnostics,
                    "dropped_as_noise": result.dropped_as_noise,
                    "findings": len(result.findings),
                    # The split that makes two runs comparable: a difference
                    # confined to model_dependent is the model, not a regression.
                    "reproducible": sum(1 for f in result.findings if f.evidence.reproducible),
                    "model_dependent": sum(
                        1 for f in result.findings if not f.evidence.reproducible
                    ),
                },
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
    )

    # Always written, even when empty: a predictable file set is easier to reason
    # about while debugging, and "the diff we collected was empty" is itself an
    # answer worth seeing.
    write(
        "diff.patch",
        "".join(f.patch_text for f in context.scope.files) if context.scope else "",
    )
    write(
        "diagnostics.json",
        json.dumps([d.model_dump() for d in context.diagnostics], indent=2, ensure_ascii=False) + "\n",
    )
    write(
        "findings.json",
        json.dumps([f.model_dump() for f in result.findings], indent=2, ensure_ascii=False) + "\n",
    )

    if context.llm_request is not None:
        write("llm/request.txt", context.llm_request)
    if context.llm_response is not None:
        write("llm/response.json", json.dumps(context.llm_response, indent=2, ensure_ascii=False) + "\n")

    log.info("run artifacts written to %s (%d file(s))", out_dir, len(written))
    return written
