"""Stage 2 — static analysis via clang-tidy (separate process, NO build).

Two operating modes:

  * compile-DB mode — a compile_commands.json was found; pass it with -p so
    clang-tidy gets real flags/includes. The target project is still never built.
  * shallow mode — no compile DB; run `clang-tidy file.cpp -- -std=c++17 -I...`
    with guessed flags. Include errors are expected and filtered out; findings
    are correspondingly lower-confidence (flagged in the report).

Diagnostics are restricted to the changed line ranges (± margin) with
clang-tidy's --line-filter.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from cpp_review_bot.analysis.checks import checks_arg
from cpp_review_bot.config import COMPILE_DB_CANDIDATES, LINE_FILTER_MARGIN, SHALLOW_MODE_STD
from cpp_review_bot.models import DiffScope, FileDiff, RawDiagnostic

log = logging.getLogger(__name__)

# e.g. "/repo/src/foo.cpp:42:13: warning: message text [concurrency-mt-unsafe]"
_DIAG_RE = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+): "
    r"(?P<level>warning|error): (?P<msg>.*?)(?: \[(?P<check>[\w\-.,]+)\])?$"
)

# Header files can't be compiled standalone in shallow mode; analyze only TUs there.
_TU_EXTENSIONS = {".cpp", ".cc", ".cxx", ".c++"}


class ClangTidyRunner:
    def __init__(self, repo: Path, profile: str = "concurrency", binary: str = "clang-tidy"):
        self.repo = repo.resolve()
        self.profile = profile
        self.binary = binary
        self.compile_db_dir = self._find_compile_db()
        self.shallow_mode = self.compile_db_dir is None

        if shutil.which(binary) is None:
            raise RuntimeError(f"{binary!r} not found on PATH — install clang-tidy first")
        if self.shallow_mode:
            log.info("no compile_commands.json found → shallow mode (guessed flags)")
        else:
            log.info("using compile DB: %s", self.compile_db_dir)

    # ------------------------------------------------------------------ API

    def run(self, scope: DiffScope) -> list[RawDiagnostic]:
        """Analyze every changed file in scope; return diagnostics that fall
        inside the changed ranges (clang-tidy enforces this via --line-filter)."""
        diagnostics: list[RawDiagnostic] = []
        for file_diff in scope.files:
            if self.shallow_mode and Path(file_diff.path).suffix.lower() not in _TU_EXTENSIONS:
                log.debug("skipping header in shallow mode: %s", file_diff.path)
                continue
            diagnostics.extend(self._run_one(file_diff))
        log.info("clang-tidy produced %d diagnostic(s) in changed ranges", len(diagnostics))
        return diagnostics

    # ------------------------------------------------------------- internals

    def _find_compile_db(self) -> Path | None:
        for candidate in COMPILE_DB_CANDIDATES:
            d = self.repo / candidate
            if (d / "compile_commands.json").is_file():
                return d
        return None

    def _line_filter(self, file_diff: FileDiff) -> str:
        lines = [
            [max(1, r.start - LINE_FILTER_MARGIN), r.end + LINE_FILTER_MARGIN]
            for r in file_diff.added_ranges
        ]
        # clang-tidy matches "name" against the *end* of the diagnostic path.
        return json.dumps([{"name": file_diff.path, "lines": lines}])

    def _command(self, file_diff: FileDiff) -> list[str]:
        cmd = [
            self.binary,
            str(self.repo / file_diff.path),
            f"--checks={checks_arg(self.profile)}",
            f"--line-filter={self._line_filter(file_diff)}",
            "--quiet",
        ]
        if self.compile_db_dir is not None:
            cmd.append(f"-p={self.compile_db_dir}")
        else:
            cmd += [
                "--",
                f"-std={SHALLOW_MODE_STD}",
                f"-I{self.repo}",
                f"-I{self.repo / 'include'}",
                f"-I{self.repo / 'src'}",
            ]
        return cmd

    def _run_one(self, file_diff: FileDiff) -> list[RawDiagnostic]:
        cmd = self._command(file_diff)
        log.debug("running: %s", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=self.repo)
        # clang-tidy exits non-zero when it emits diagnostics or hits compile
        # errors; both are expected, so we always parse stdout.
        return self._parse_output(proc.stdout)

    def _parse_output(self, output: str) -> list[RawDiagnostic]:
        diagnostics: list[RawDiagnostic] = []
        dropped_compile_errors = 0
        for line in output.splitlines():
            m = _DIAG_RE.match(line.strip())
            if not m:
                continue
            check = m.group("check") or ""
            # In shallow mode, missing headers produce clang-diagnostic-error
            # noise that isn't a review finding — drop it.
            if m.group("level") == "error" and (not check or check.startswith("clang-diagnostic")):
                dropped_compile_errors += 1
                continue
            file_path = m.group("file")
            try:
                file_path = str(Path(file_path).resolve().relative_to(self.repo))
            except ValueError:
                pass  # outside the repo (system header) — keep as-is
            diagnostics.append(
                RawDiagnostic(
                    file=file_path,
                    line=int(m.group("line")),
                    column=int(m.group("col")),
                    level=m.group("level"),
                    check=check,
                    message=m.group("msg"),
                )
            )
        if dropped_compile_errors:
            log.debug("dropped %d compile error(s) (expected in shallow mode)", dropped_compile_errors)
        return diagnostics
