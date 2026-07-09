"""CLI entrypoint — wires the four pipeline stages together.

    cpp-review --repo /path/to/project --base main --out report.md
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from cpp_review_bot.analysis import CHECK_PROFILES, ClangTidyRunner
from cpp_review_bot.config import DEFAULT_MODEL, setup_logging
from cpp_review_bot.diff import collect_diff
from cpp_review_bot.models import Finding, ReviewResult
from cpp_review_bot.report import render_markdown

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cpp-review",
        description="Diff-scoped C++ review: clang-tidy + LLM triage, no build required.",
    )
    p.add_argument("--repo", type=Path, default=Path("."), help="target git repo (default: cwd)")
    p.add_argument("--base", default=None, help="base ref to diff against (e.g. main); omit for working-tree changes")
    p.add_argument("--profile", default="concurrency", choices=sorted(CHECK_PROFILES), help="check profile")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model for triage (default: {DEFAULT_MODEL})")
    p.add_argument("--no-llm", action="store_true", help="skip LLM triage; emit raw clang-tidy findings")
    p.add_argument("--out", type=Path, default=None, help="write report to file (default: stdout)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def run_pipeline(args: argparse.Namespace) -> ReviewResult:
    # Stage 1 — diff
    scope = collect_diff(args.repo, args.base)
    if scope.is_empty:
        log.warning("no C++ changes found in the diff — nothing to analyze")
        return ReviewResult(base_ref=args.base, profile=args.profile)

    # Stage 2 — static analysis
    runner = ClangTidyRunner(args.repo, profile=args.profile)
    diagnostics = runner.run(scope)

    result = ReviewResult(
        base_ref=args.base,
        profile=args.profile,
        shallow_mode=runner.shallow_mode,
        total_diagnostics=len(diagnostics),
    )

    # Stage 3 — LLM triage (optional)
    use_llm = not args.no_llm
    if use_llm and not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY not set — falling back to --no-llm behavior")
        use_llm = False

    if use_llm:
        # Imported lazily so --no-llm works without the anthropic package configured.
        from cpp_review_bot.llm import LlmPostProcessor

        processor = LlmPostProcessor(model=args.model)
        result.findings, result.dropped_as_noise = processor.process(
            scope, diagnostics, shallow_mode=runner.shallow_mode
        )
        result.llm_used = True
    else:
        result.findings = [
            Finding(file=d.file, line=d.line, check=d.check, title=d.message, explanation=d.message)
            for d in diagnostics
        ]
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)

    try:
        result = run_pipeline(args)
    except Exception as exc:  # surface a clean error instead of a traceback wall
        log.error("%s", exc, exc_info=args.verbose)
        return 1

    # Stage 4 — report
    report = render_markdown(result)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
        log.info("report written to %s", args.out)
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
