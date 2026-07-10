"""Shared data models — the contracts between pipeline stages.

Data flow:

    git diff ──▶ DiffScope ──▶ list[RawDiagnostic] ──▶ list[Finding] ──▶ report.md
       (diff.collector)  (analysis.clang_tidy)   (llm.postprocess)   (report.markdown)
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class LineRange(BaseModel):
    """Inclusive 1-based line range in the *new* version of a file."""

    start: int
    end: int


class FileDiff(BaseModel):
    """One changed C++ file: which new-side lines changed, plus the raw patch
    text used later as LLM context."""

    path: str
    added_ranges: list[LineRange] = Field(default_factory=list)
    patch_text: str = ""


class DiffScope(BaseModel):
    """Everything the rest of the pipeline needs to know about the diff."""

    base_ref: str | None = None
    files: list[FileDiff] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any(f.added_ranges for f in self.files)


class RawDiagnostic(BaseModel):
    """A single clang-tidy diagnostic, before LLM triage."""

    file: str
    line: int
    column: int = 0
    level: str = "warning"  # warning | error
    check: str = ""  # e.g. "concurrency-mt-unsafe"
    message: str


class Severity(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


class Finding(BaseModel):
    """A triaged review finding, ready for the report.

    Produced either by LLM triage of a RawDiagnostic, or directly by the LLM
    from diff context (e.g. lock-order inversions clang-tidy can't see).
    """

    file: str
    line: int
    check: str = ""  # clang-tidy check name, or "llm-review" for LLM-originated
    severity: Severity = Severity.medium
    title: str
    explanation: str
    suggestion: str = ""  # human-readable fix proposal (may contain a code block)
    source: str = "clang-tidy"  # "clang-tidy" | "llm" | "convention"


class ReviewResult(BaseModel):
    """Final pipeline output handed to the report renderer."""

    base_ref: str | None = None
    profile: str = "concurrency"
    shallow_mode: bool = False
    llm_used: bool = False
    conventions_loaded: int = 0  # adopted rules loaded from conventions.yml
    total_diagnostics: int = 0
    dropped_as_noise: int = 0
    findings: list[Finding] = Field(default_factory=list)
