"""Global configuration and logging setup.

API keys are NEVER stored here — each provider's SDK reads its own key from
the environment (ANTHROPIC_API_KEY / OPENAI_API_KEY). The active provider is
chosen by the LLM_PROVIDER environment variable; `.env` in the target cwd is
loaded by the CLI entrypoint (real environment variables win — override=False).
Design: docs/llm-provider-and-keys-design.md.
"""

from __future__ import annotations

import logging
import os
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

# --- LLM provider selection (docs/llm-provider-and-keys-design.md) -----------

# The provider is chosen at runtime by LLM_PROVIDER; per-stage default models
# derive from it. Rationale for the anthropic tiers is in
# docs/convention-detection-design.md §4: review (diff triage) needs nuanced
# context reasoning and precision is the product's survival metric → strong
# model; convention learning is structured-stats-in / structured-rules-out
# classification → the cheaper tier suffices. The openai tiers mirror that split.
DEFAULT_PROVIDER = "anthropic"

PROVIDER_MODELS = {
    "anthropic": {"review": "claude-opus-4-8", "learn": "claude-sonnet-5"},
    "openai": {"review": "gpt-4o", "learn": "gpt-4o-mini"},
}

PROVIDER_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def current_provider() -> str:
    """The active LLM provider, from LLM_PROVIDER (default: anthropic)."""
    provider = os.environ.get("LLM_PROVIDER", DEFAULT_PROVIDER).strip().lower()
    if provider not in PROVIDER_MODELS:
        raise ValueError(
            f"unknown LLM_PROVIDER {provider!r} — expected one of {sorted(PROVIDER_MODELS)}"
        )
    return provider


def default_review_model() -> str:
    return PROVIDER_MODELS[current_provider()]["review"]


def default_learn_model() -> str:
    return PROVIDER_MODELS[current_provider()]["learn"]


def required_key_env() -> str:
    """Name of the env var holding the active provider's API key."""
    return PROVIDER_KEY_ENV[current_provider()]


def has_api_key() -> bool:
    return bool(os.environ.get(required_key_env()))

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
    # The SDKs' HTTP layers are noisy at DEBUG; keep them at WARNING unless needed.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.INFO if verbose else logging.WARNING)
    logging.getLogger("openai").setLevel(logging.INFO if verbose else logging.WARNING)
