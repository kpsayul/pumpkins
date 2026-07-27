"""Shared data models — the contracts between pipeline stages.

Data flow:

    git diff ──▶ DiffScope ──▶ list[RawDiagnostic] ──▶ list[Finding] ──▶ report.md
       (diff.collector)  (analysis.clang_tidy)   (llm.postprocess)   (report.markdown)

Every Finding carries an `Evidence`: which rule produced it, which detector
judged it, and whether the same input would produce it again. The convention
checker (conventions.checker) is a third producer alongside the two above.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


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
    # Changed files dropped for not having a recognized C++ extension. Kept so
    # the report can say what it did not look at, instead of implying it did.
    skipped_files: list[str] = Field(default_factory=list)

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


class DetectorKind(str, Enum):
    """Who judged a finding."""

    clang_tidy = "clang-tidy"
    convention = "convention"  # deterministic match against conventions/rules/
    llm = "llm"


# Detectors that return the same answer for the same input. Only these may ever
# gate CI: an LLM verdict that fails a build and then passes on re-run is how a
# review tool gets switched off. Observed in practice — the same diff, model and
# rules produced 0 findings on one run and 1 on the next.
DETERMINISTIC_DETECTORS = frozenset({DetectorKind.clang_tidy, DetectorKind.convention})


class Evidence(BaseModel):
    """Why a finding exists — the part a machine can read.

    Before this existed the answer lived in two overloaded strings (a `check`
    field holding three different formats) and in prose inside the explanation,
    so nothing could ask "which rule fired", "is this reproducible", or "did two
    runs differ only in the unstable findings".

    `reproducible` is derived from `detector` unless stated, so a new producer
    cannot forget it or contradict itself.
    """

    detector: DetectorKind
    rule_id: str | None = None  # None when nothing but the model's own judgement backs it
    reproducible: bool = True

    # Backing numbers for a convention rule (were prose in the explanation).
    occurrences: int | None = None
    coverage: float | None = None
    rule_scope: str | None = None

    # Which model judged it, for llm findings.
    model: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _derive_reproducible(cls, data: object) -> object:
        if isinstance(data, dict) and data.get("reproducible") is None and "detector" in data:
            detector = DetectorKind(data["detector"])
            return {**data, "reproducible": detector in DETERMINISTIC_DETECTORS}
        return data


class Finding(BaseModel):
    """A triaged review finding, ready for the report.

    Produced by clang-tidy triage, by the LLM reading the diff directly, or by
    the deterministic convention check. `evidence` is required — a finding
    without provenance is exactly what this model exists to prevent.
    """

    file: str
    line: int
    severity: Severity = Severity.medium
    title: str
    explanation: str
    suggestion: str = ""  # human-readable fix proposal (may contain a code block)
    evidence: Evidence


class ReviewResult(BaseModel):
    """Final pipeline output handed to the report renderer."""

    base_ref: str | None = None
    profile: str = "concurrency"
    shallow_mode: bool = False
    llm_used: bool = False
    # Which model produced the LLM-judged findings. Recorded because a result you
    # cannot attribute to a model is a result you cannot compare across models.
    provider: str | None = None
    model: str | None = None
    temperature: float | None = None  # sampling setting also changes the result
    conventions_loaded: int = 0  # active rules enforced this run
    # Candidates awaiting a human decision. Reported because a rule sitting in
    # conventions/candidates/ looks learned but is deliberately not enforced —
    # without saying so, its absence from the findings reads as a pass.
    conventions_pending: int = 0
    total_diagnostics: int = 0
    dropped_as_noise: int = 0
    findings: list[Finding] = Field(default_factory=list)

    # What the run could NOT look at. Without this a report showing zero
    # findings is indistinguishable from a report that never read the change,
    # which is the one failure mode a review tool must never have.
    analyzed_files: int = 0
    skipped_non_cpp: list[str] = Field(default_factory=list)
    skipped_headers: list[str] = Field(default_factory=list)

    @property
    def has_coverage_gap(self) -> bool:
        return bool(self.skipped_non_cpp or self.skipped_headers)
