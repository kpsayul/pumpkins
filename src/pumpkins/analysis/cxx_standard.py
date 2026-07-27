"""What C++ standard does this project promise to still compile under?

The portability profile needs this as an input, not as a guess. A construct is
only a defect relative to a declared minimum: `inline constexpr` at namespace
scope is unremarkable in a C++17 project and breaks the build in a C++11 one.
Asking the model to infer the minimum from a diff would be asking it to invent
the very thing the judgement rests on.

So it is read mechanically from what the project declares — the build config
and the CI matrix — and the *lowest* value wins, because that is the one that
has to keep compiling.

Deliberately shallow: a few well-known declaration sites, no CMake evaluation.
When nothing is found the profile says so rather than assuming a default; a
wrong minimum would turn every modern construct into a false positive.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# `target_compile_features(fmt PUBLIC cxx_std_11)` — the strongest signal,
# since it is the project stating its own contract.
_CXX_STD_FEATURE_RE = re.compile(r"\bcxx_std_(\d{2})\b")
# `set(CMAKE_CXX_STANDARD 17)` / `CMAKE_CXX_STANDARD: 17`, but not
# `-DCMAKE_CXX_STANDARD=${{matrix.std}}` where the value is a variable.
_CMAKE_STANDARD_RE = re.compile(r"CMAKE_CXX_STANDARD[\s:=]+(\d{2})\b")
# `-std=c++17`, `-std=gnu++17`
_STD_FLAG_RE = re.compile(r"-std=(?:c|gnu)\+\+(\d{2})\b")
# CI matrix entries: `std: [11]`, `std: 14`, `standard: [17, 20]`
_CI_MATRIX_RE = re.compile(r"\b(?:std|standard)\s*:\s*(\[[^\]]*\]|\d{2})")

KNOWN_STANDARDS = (11, 14, 17, 20, 23, 26)
MAX_WORKFLOW_FILES = 12


@dataclass(frozen=True)
class CxxStandard:
    """The lowest standard the project declares support for, and where it said so."""

    minimum: int
    sources: list[str]  # repo-relative paths, for the prompt and for provenance

    def describe(self) -> str:
        return f"C++{self.minimum} (근거: {', '.join(self.sources)})"


def _scan(text: str) -> set[int]:
    found: set[int] = set()
    for regex in (_CXX_STD_FEATURE_RE, _CMAKE_STANDARD_RE, _STD_FLAG_RE):
        found.update(int(m) for m in regex.findall(text))
    for raw in _CI_MATRIX_RE.findall(text):
        found.update(int(n) for n in re.findall(r"\d{2}", raw))
    return {n for n in found if n in KNOWN_STANDARDS}


def _candidate_files(repo: Path) -> list[Path]:
    files = [repo / "CMakeLists.txt"]
    workflows = repo / ".github" / "workflows"
    if workflows.is_dir():
        files += sorted(workflows.glob("*.y*ml"))[:MAX_WORKFLOW_FILES]
    return [p for p in files if p.is_file()]


def detect_cxx_standard(repo: Path) -> CxxStandard | None:
    """Lowest declared C++ standard, or None when the project does not say."""
    found: dict[int, list[str]] = {}
    for path in _candidate_files(repo):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.debug("could not read %s: %s", path, exc)
            continue
        for standard in _scan(text):
            found.setdefault(standard, []).append(str(path.relative_to(repo)))

    if not found:
        log.info("no declared C++ standard found — portability checks will say so")
        return None

    minimum = min(found)
    result = CxxStandard(minimum=minimum, sources=found[minimum])
    log.info(
        "declared C++ standard: minimum C++%d (from %s; all declared: %s)",
        minimum, ", ".join(result.sources), sorted(found),
    )
    return result
