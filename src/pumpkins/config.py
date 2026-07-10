"""Global configuration and logging setup.

The Anthropic API key is NEVER stored here — it is read from the
ANTHROPIC_API_KEY environment variable by the `anthropic` SDK itself.
"""

from __future__ import annotations

import logging
import sys

# --- Pipeline constants -----------------------------------------------------

# File extensions treated as C++ translation units / headers.
CPP_EXTENSIONS = {".cpp", ".cc", ".cxx", ".c++", ".h", ".hpp", ".hh", ".hxx", ".inl"}

# Extra lines around each changed range passed to clang-tidy's --line-filter.
# Concurrency bugs (lock ordering, unguarded member access) usually need the
# surrounding function for context, so we widen the reported window a bit.
LINE_FILTER_MARGIN = 15

# Default C++ standard used in shallow mode (no compile_commands.json).
SHALLOW_MODE_STD = "c++17"

# Per-stage default models — rationale in docs/convention-detection-design.md §4.
# Review (diff triage) needs nuanced context reasoning and precision is the
# product's survival metric → Opus. Convention learning (`learn`, planned) is
# structured-stats-in / structured-rules-out classification → Sonnet suffices.
DEFAULT_REVIEW_MODEL = "claude-opus-4-8"
DEFAULT_LEARN_MODEL = "claude-sonnet-5"

# Candidate locations for compile_commands.json, relative to the repo root.
COMPILE_DB_CANDIDATES = [".", "build", "out", "cmake-build-debug", "cmake-build-release"]

# --- Convention learning (docs/convention-detection-design.md) ---------------

# Numeric definition of "a convention": a pattern is adopted as a rule only if
# it occurs at least MIN_RULE_OCCURRENCES times AND covers at least
# MIN_RULE_CONSISTENCY of its category (design doc §3-(2)). Enforced in code,
# not just in the LLM prompt.
MIN_RULE_OCCURRENCES = 20
MIN_RULE_CONSISTENCY = 0.85

# Where `pumpkins learn` writes its human-reviewable artifact, relative to the
# target repo root — meant to be committed alongside the code (§3-(1)).
CONVENTIONS_FILENAME = "conventions.yml"

# Directories never scanned for conventions (vendored/generated code has
# someone else's conventions).
LEARN_SKIP_DIRS = {
    ".git", "build", "out", "cmake-build-debug", "cmake-build-release",
    "third_party", "3rdparty", "external", "vendor", "deps", "node_modules", ".venv",
}


def setup_logging(verbose: bool = False) -> None:
    """Configure root logging for the CLI. Logs go to stderr so stdout stays
    clean for piping the markdown report."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # The SDK's HTTP layer is noisy at DEBUG; keep it at WARNING unless needed.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.INFO if verbose else logging.WARNING)
