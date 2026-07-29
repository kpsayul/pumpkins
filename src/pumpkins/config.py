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

# C++ syntax — including which extensions count — lives in pumpkins/languages/cpp/.
# A repo can override the extension set; see languages/__init__.py.

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

# Sampling temperature per stage. None means "omit the parameter", so the
# provider's own default applies.
#
# Review is pinned to 0: its output goes straight to the user, and with the API
# default (1.0) the same diff, model and rules produced 0 findings on one run
# and 1 on the next. The variance concentrates on borderline judgements, which
# are disproportionately false positives — a `constexpr` array read without a
# mutex matched "shared data accessed without a lock" half the time even though
# it is read-only. Note this reduces flapping but does not remove it: neither
# provider guarantees determinism at temperature 0, which is why findings still
# carry a `reproducible` label rather than a promise.
#
# Learn keeps the default on purpose: it judges statistical patterns and is
# asked to notice sample-based conventions the canned facets cannot express, so
# some exploration helps. Its output is protected by the code-side threshold
# gate, the reconcile step and human approval before anything is enforced.
REVIEW_TEMPERATURE: float | None = 0.0
LEARN_TEMPERATURE: float | None = None


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


def default_reasoning_model() -> str:
    """Model for learn's *reasoning* sub-task — naming a hidden split's boundary.

    This is the strong (review-tier) model on purpose. §4.1 measured that the
    cheap learn tier reads the two groups' directories and still answers "no
    structural distinction", while the strong tier says "test/gtest vs
    include/fmt": same input, split answer. So the two-stage learner (design doc
    §4.3) keeps classification on the cheap tier and escalates only this
    reasoning step here. Same model as review; named apart so the intent reads."""
    return PROVIDER_MODELS[current_provider()]["review"]


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
# A directory in the *target* repo, one file per rule, with each rule's status
# expressed by which subdirectory it sits in (conventions/store.py explains why).
# Named after the tool rather than after "conventions": the latter is a word
# repos already use for their own docs and namespaces, and a tool that plants a
# directory in someone else's project should not squat on a generic name.
PUMPKINS_DIRNAME = "pumpkins"
# Pre-directory layout. Still *read* so existing repos keep working; never written.
CONVENTIONS_FILENAME = "conventions.yml"

# Structural check kinds the review side enforces deterministically
# (conventions/checker.check_structural). Single source of truth: the checker
# reads it to decide what to run, and the LLM prompt reads it to decide what NOT
# to re-report. When those two lists drifted apart, adding a check kind meant the
# model repeated every finding the checker had already made.
#
# `DETERMINISTIC_STRUCTURAL_AST` is the subset that needs tree-sitter. Layering
# is deliberately outside it — an `#include` is lexically unambiguous, so the
# rule most likely to matter survives a machine where the native parser is broken.
DETERMINISTIC_STRUCTURAL_CHECKS = frozenset(
    {"return_type", "member_ownership", "base_class", "include_direction"}
)
DETERMINISTIC_STRUCTURAL_AST = frozenset(
    {"return_type", "member_ownership", "base_class"}
)

# A rule's status IS the subdirectory it lives in, so the two can never drift
# and `git mv` records who changed it. Only `rules/` is enforced by a review.
RULE_STATUS_DIRS = {"active": "rules", "candidate": "candidates", "archived": "archive"}

# Directories never scanned for conventions (vendored/generated code has
# someone else's conventions).
LEARN_SKIP_DIRS = {
    ".git", "build", "out", "cmake-build-debug", "cmake-build-release",
    "third_party", "3rdparty", "external", "vendor", "deps", "node_modules", ".venv",
}

# Test directories, skipped by default and re-enabled with `learn --include-tests`.
# Two reasons, both measured: test scaffolding follows looser naming than the
# library it exercises, and vendored test frameworks hide here under names the
# list above doesn't catch — in fmt, `test/gtest/` (bundled googletest) supplied
# 2837 of the 2845 UpperCamel function/class names and made a uniformly
# snake_case codebase look 63% UpperCamel.
LEARN_TEST_DIRS = {"test", "tests", "testing", "unittest", "unittests"}


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
