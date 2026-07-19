"""CLI entrypoint.

    pumpkins --repo /path/to/project --base main --out report.md   # diff review
    pumpkins learn --repo /path/to/project                         # learn conventions.yml

The bare command runs the review pipeline (backward compatible); `learn` is
dispatched as a subcommand before argparse sees the rest.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from pumpkins.analysis import CHECK_PROFILES, ClangTidyRunner
from pumpkins.config import (
    CONVENTIONS_FILENAME,
    current_provider,
    default_learn_model,
    default_review_model,
    has_api_key,
    required_key_env,
    setup_logging,
)
from pumpkins.diff import collect_diff
from pumpkins.models import Finding, ReviewResult, Severity
from pumpkins.report import render_markdown

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pumpkins",
        description="Diff-scoped C++ review: clang-tidy + LLM triage, no build required.",
        epilog="subcommand: `pumpkins learn --repo PATH` learns the repo's naming "
        "conventions into conventions.yml (see `pumpkins learn -h`)",
    )
    p.add_argument("--repo", type=Path, default=Path("."), help="target git repo (default: cwd)")
    p.add_argument("--base", default=None, help="base ref to diff against (e.g. main); omit for working-tree changes")
    p.add_argument("--profile", default="concurrency", choices=sorted(CHECK_PROFILES), help="check profile")
    p.add_argument(
        "--model",
        default=default_review_model(),
        help=f"review triage model (default for LLM_PROVIDER={current_provider()}: "
        f"{default_review_model()})",
    )
    p.add_argument("--no-llm", action="store_true", help="skip LLM triage; emit raw clang-tidy findings")
    p.add_argument(
        "--conventions",
        type=Path,
        default=None,
        help=f"conventions file to check the diff against "
        f"(default: <repo>/{CONVENTIONS_FILENAME} if present; see `pumpkins learn`)",
    )
    p.add_argument(
        "--no-conventions", action="store_true", help="skip the convention check stage"
    )
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
    if use_llm and not has_api_key():
        log.warning(
            "%s not set (LLM_PROVIDER=%s) — falling back to --no-llm behavior",
            required_key_env(), current_provider(),
        )
        use_llm = False

    if use_llm:
        # Imported lazily so --no-llm works without the provider SDK configured.
        from pumpkins.llm import LlmPostProcessor

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

    # Stage 3.5 — convention check against conventions.yml (deterministic — runs
    # with or without an API key; see docs/convention-detection-design.md §6-2)
    if not args.no_conventions:
        conv_path = args.conventions or (args.repo / CONVENTIONS_FILENAME)
        if conv_path.exists():
            from pumpkins.conventions import check_scope, load_conventions

            rules = load_conventions(conv_path)
            result.conventions_loaded = len(rules)
            conv_findings = check_scope(scope, rules)
            result.findings.extend(conv_findings)
            log.info(
                "conventions: %d rule(s) from %s → %d finding(s)",
                len(rules), conv_path, len(conv_findings),
            )
        elif args.conventions:
            raise RuntimeError(f"conventions file not found: {conv_path}")
        else:
            log.debug("no %s in repo — convention check skipped", CONVENTIONS_FILENAME)

    # final ordering across all sources (clang-tidy / llm / convention)
    order = list(Severity)
    result.findings.sort(key=lambda f: (order.index(f.severity), f.file, f.line))
    return result


# ------------------------------------------------------------ learn command

def build_learn_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pumpkins learn",
        description="Scan a repo's existing C++ code and distill its implicit "
        "naming conventions into a human-reviewable conventions.yml "
        "(docs/convention-detection-design.md).",
    )
    p.add_argument("--repo", type=Path, default=Path("."), help="target git repo (default: cwd)")
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"output path (default: <repo>/{CONVENTIONS_FILENAME})",
    )
    p.add_argument(
        "--model",
        default=default_learn_model(),
        help=f"rule judgment model (default for LLM_PROVIDER={current_provider()}: "
        f"{default_learn_model()})",
    )
    p.add_argument(
        "--no-llm",
        action="store_true",
        help="dump the raw identifier statistics only (pipeline debugging)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def run_learn(argv: list[str]) -> int:
    args = build_learn_parser().parse_args(argv)
    setup_logging(args.verbose)

    # Imported lazily, like the review pipeline's LLM stage, so the review
    # path never pays for pyyaml/provider-SDK imports it doesn't use.
    from pumpkins.conventions import (
        ConventionLearner,
        extract_stats,
        render_conventions_yaml,
        render_stats_yaml,
    )

    try:
        stats = extract_stats(args.repo)
        if sum(s.total for s in stats) == 0:
            log.error("no C++ identifiers found under %s — nothing to learn", args.repo)
            return 1

        if args.no_llm:
            text = render_stats_yaml(stats)
            if args.out:
                args.out.write_text(text, encoding="utf-8")
                log.info("stats written to %s", args.out)
            else:
                print(text)
            return 0

        if not has_api_key():
            log.error(
                "%s not set (LLM_PROVIDER=%s) — rule judgment needs the LLM "
                "(use --no-llm to inspect the raw statistics)",
                required_key_env(), current_provider(),
            )
            return 1

        learner = ConventionLearner(model=args.model)
        result = learner.learn(stats)
        out = args.out or (args.repo / CONVENTIONS_FILENAME)
        out.write_text(render_conventions_yaml(args.repo, args.model, stats, result), encoding="utf-8")
        log.info(
            "%d rule(s) adopted, %d candidate(s) rejected — review and commit %s",
            len(result.rules),
            len(result.rejected),
            out,
        )
    except Exception as exc:  # surface a clean error instead of a traceback wall
        log.error("%s", exc, exc_info=args.verbose)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    # .env fills in LLM_PROVIDER / API keys for people who prefer a file over
    # shell exports; real environment variables win (override=False).
    # usecwd=True: search from the invocation directory upward — without it,
    # find_dotenv would search from this installed file's location instead.
    load_dotenv(find_dotenv(usecwd=True))
    try:
        current_provider()  # fail fast on a bad LLM_PROVIDER before argparse defaults resolve
    except ValueError as exc:
        print(f"pumpkins: {exc}", file=sys.stderr)
        return 2

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "learn":
        return run_learn(argv[1:])

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
