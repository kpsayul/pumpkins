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

from pumpkins.analysis import ClangTidyRunner
from pumpkins.analysis.cxx_standard import detect_cxx_standard
from pumpkins.config import (
    CONVENTIONS_DIRNAME,
    REVIEW_TEMPERATURE,
    CONVENTIONS_FILENAME,
    LEARN_TEST_DIRS,
    RULE_STATUS_DIRS as STATUS_DIRS,
    current_provider,
    default_learn_model,
    default_review_model,
    has_api_key,
    required_key_env,
    setup_logging,
)
from pumpkins.diff import collect_diff
from pumpkins.profiles import DEFAULT_PROFILE, PROFILES
from pumpkins.models import DetectorKind, Evidence, Finding, ReviewResult, Severity
from pumpkins.report import render_markdown
from pumpkins.report.dump import RunContext, dump_run

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
    p.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        choices=sorted(PROFILES),
        help="what to look for — "
        + ", ".join(f"{n}: {p.description}" for n, p in sorted(PROFILES.items())),
    )
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
        help=f"rules to check the diff against — a {CONVENTIONS_DIRNAME}/ directory "
        f"or a legacy {CONVENTIONS_FILENAME} (default: whichever exists in the repo; "
        f"see `pumpkins learn`)",
    )
    p.add_argument(
        "--no-conventions", action="store_true", help="skip the convention check stage"
    )
    p.add_argument("--out", type=Path, default=None, help="write report to file (default: stdout)")
    p.add_argument(
        "--out-dir",
        type=Path,
        nargs="?",
        const=Path("out"),
        default=None,
        metavar="DIR",
        help="also dump the report plus debug artifacts (diff, raw diagnostics, "
        "LLM prompt/response, run provenance) into DIR — bare flag means ./out/",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def _default_conventions_path(repo: Path) -> Path | None:
    """Prefer the conventions/ store; fall back to a pre-directory single file."""
    for candidate in (repo / CONVENTIONS_DIRNAME, repo / CONVENTIONS_FILENAME):
        if candidate.exists():
            return candidate
    return None


def run_pipeline(args: argparse.Namespace) -> tuple[ReviewResult, RunContext]:
    """Returns the report contract plus the raw inputs, for --out-dir."""
    context = RunContext()

    # Stage 1 — diff
    scope = collect_diff(args.repo, args.base)
    context.scope = scope
    if scope.is_empty:
        log.warning("no C++ changes found in the diff — nothing to analyze")
        return ReviewResult(
            base_ref=args.base,
            profile=args.profile,
            skipped_non_cpp=scope.skipped_files,
        ), context

    # Stage 2 — static analysis
    runner = ClangTidyRunner(args.repo, profile=args.profile)
    diagnostics = runner.run(scope)
    context.diagnostics = diagnostics
    context.tool_version = runner.tool_version

    result = ReviewResult(
        base_ref=args.base,
        profile=args.profile,
        shallow_mode=runner.shallow_mode,
        total_diagnostics=len(diagnostics),
        analyzed_files=runner.analyzed_files,
        skipped_non_cpp=scope.skipped_files,
        skipped_headers=runner.skipped_headers,
    )

    # Rules are loaded BEFORE the LLM stage so they can go into its prompt: the
    # model judges the rules a regex cannot express (facet: other), while the
    # deterministic checker below keeps the ones it can. Design doc §2 방안 B.
    rules: list = []
    conv_path = args.conventions or _default_conventions_path(args.repo)
    if not args.no_conventions and conv_path is not None and conv_path.exists():
        from pumpkins.conventions import count_candidates, load_conventions

        context.conventions_path = conv_path
        rules = load_conventions(conv_path)
        result.conventions_loaded = len(rules)
        result.conventions_pending = count_candidates(conv_path)
    elif args.conventions is not None and not args.conventions.exists():
        raise RuntimeError(f"conventions path not found: {args.conventions}")
    elif not args.no_conventions:
        log.debug(
            "no %s/ or %s in repo — convention check skipped",
            CONVENTIONS_DIRNAME, CONVENTIONS_FILENAME,
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

        processor = LlmPostProcessor(model=args.model, profile=args.profile)
        cxx_standard = (
            detect_cxx_standard(args.repo)
            if processor.profile.needs_cxx_standard
            else None
        )
        result.findings, result.dropped_as_noise = processor.process(
            scope,
            diagnostics,
            shallow_mode=runner.shallow_mode,
            rules=rules,
            cxx_standard=cxx_standard,
        )
        result.llm_used = True
        result.provider, result.model = current_provider(), processor.model
        result.temperature = REVIEW_TEMPERATURE
        context.llm_request, context.llm_response = (
            processor.last_request, processor.last_response,
        )
    else:
        # Untriaged clang-tidy output: no model touched it, so these are the
        # only findings the pipeline can currently call fully reproducible.
        result.findings = [
            Finding(
                file=d.file,
                line=d.line,
                title=d.message,
                explanation=d.message,
                evidence=Evidence(
                    detector=DetectorKind.clang_tidy, rule_id=d.check or None
                ),
            )
            for d in diagnostics
        ]

    # Stage 3.5 — deterministic convention check on the rules loaded above.
    # Runs with or without an API key (design doc §6-2); the LLM stage above
    # covered the rules this one cannot express.
    if rules:
        from pumpkins.conventions import check_scope

        conv_findings = check_scope(scope, rules)
        result.findings.extend(conv_findings)
        log.info(
            "conventions: %d rule(s) from %s → %d finding(s)",
            len(rules), conv_path, len(conv_findings),
        )

    # final ordering across all sources (clang-tidy / llm / convention)
    order = list(Severity)
    result.findings.sort(key=lambda f: (order.index(f.severity), f.file, f.line))
    return result, context


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
        help=f"rule store directory (default: <repo>/{CONVENTIONS_DIRNAME}/). "
        f"With --no-llm this is a file for the statistics dump instead",
    )
    p.add_argument(
        "--accept-all",
        action="store_true",
        help=f"write adopted rules straight into {CONVENTIONS_DIRNAME}/rules/ instead "
        "of candidates/. Skips the review step — use for a first run you intend "
        "to accept wholesale",
    )
    p.add_argument(
        "--reconsider",
        action="store_true",
        help=f"also re-propose rules previously moved to {CONVENTIONS_DIRNAME}/archive/ "
        "(they are suppressed by default: a rejection is a decision, not an absence)",
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
    p.add_argument(
        "--include",
        action="append",
        metavar="GLOB",
        help="only scan paths matching this glob (repeatable). Rules learned "
        "from a subtree are scoped to it, e.g. --include 'src/legacy/**'",
    )
    p.add_argument(
        "--exclude",
        action="append",
        metavar="GLOB",
        help="skip paths matching this glob (repeatable) — use for generated or "
        "vendored code the built-in skip list misses",
    )
    p.add_argument(
        "--include-tests",
        action="store_true",
        help=f"also scan test directories ({', '.join(sorted(LEARN_TEST_DIRS))}), "
        "which are skipped by default because they hold looser naming and "
        "bundled test frameworks",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def _format_reconciliation(root, rec, written, accept_all: bool) -> str:
    """The human-facing output of `learn` — what changed and what needs a decision.

    This *is* the approval surface: learn proposes, the summary says exactly
    which files to move. Non-interactive on purpose so it works in CI and so the
    decision lands in a reviewable commit rather than a terminal session.
    """
    target = STATUS_DIRS["active"] if accept_all else STATUS_DIRS["candidate"]
    lines = [f"규칙 저장소: {root}", ""]

    if rec.new_candidates:
        lines.append(f"신규 {len(rec.new_candidates)}건 → {root.name}/{target}/")
        for rule in rec.new_candidates:
            lines.append(
                f"  + {rule.id}  —  {rule.description} "
                f"({rule.coverage:.0%}, {rule.occurrences}개)"
            )
    if rec.refreshed:
        lines.append(f"근거 수치만 갱신 {len(rec.refreshed)}건 (결정과 이유는 보존)")
        for rule in rec.refreshed:
            lines.append(f"  ~ {rule.id}  →  {rule.coverage:.0%}, {rule.occurrences}개")
    if rec.superseded:
        lines.append(
            f"대체 제안 {len(rec.superseded)}건 — 같은 category/facet의 값이 달라졌습니다"
        )
        for old, new in rec.superseded:
            lines.append(f"  ! {old} (활성)  →  {new} (후보)")
    if rec.stale:
        lines.append(
            f"은퇴 후보 {len(rec.stale)}건 — 이번 스캔이 더 이상 뒷받침하지 못합니다 "
            f"(자동 삭제하지 않았습니다)"
        )
        for rule_id in rec.stale:
            lines.append(f"  - {rule_id}")
    if rec.suppressed:
        lines.append(
            f"이전에 기각한 {len(rec.suppressed)}건은 다시 제안하지 않았습니다 "
            f"(--reconsider로 재검토)"
        )
    if rec.unchanged:
        lines.append(f"변화 없음 {len(rec.unchanged)}건")

    if not (rec.needs_attention or rec.refreshed):
        lines.append("변경 사항이 없습니다 — 기존 결정을 그대로 유지했습니다.")

    if written["candidates"] and not accept_all:
        lines += [
            "",
            "검수 후 파일을 옮기면 결정이 됩니다 (git이 누가 언제 승인했는지 기록합니다):",
            f"  승인:  git mv {root.name}/{STATUS_DIRS['candidate']}/<file> "
            f"{root.name}/{STATUS_DIRS['active']}/",
            f"  기각:  git mv {root.name}/{STATUS_DIRS['candidate']}/<file> "
            f"{root.name}/{STATUS_DIRS['archived']}/    # reason에 이유를 남겨 두세요",
            f"승인 전까지 후보는 리뷰에 적용되지 않습니다.",
        ]
    return "\n".join(lines)


def _has_split_signal(stats) -> bool:
    """Whether any category looks like two groups rather than one convention."""
    from pumpkins.config import MIN_RULE_CONSISTENCY
    from pumpkins.conventions import detect_split_signal

    return any(
        detect_split_signal(counts, total, MIN_RULE_CONSISTENCY)
        for s in stats
        for counts, total in (
            (s.prefix_counts, s.total),
            (s.suffix_counts, s.total),
            (s.casing_counts, s.casing_informative),
        )
    )


def _format_split_hypotheses(hypotheses, root) -> str:
    """Categories that failed the threshold because they may hold two groups.

    Not rules and never enforced — a question for a human. Acting on one means
    either splitting the category in the extractor (when the boundary is
    mechanically visible) or writing a scoped rule by hand.
    """
    lines = ["", f"규칙이 안 된 이유를 다시 볼 만한 것 {len(hypotheses)}건:"]
    for h in hypotheses:
        checkable = "기계 확인 가능" if h.checkable else "사람 판단 필요"
        lines.append(f"  ? {h.category}.{h.facet} — {h.groups}")
        if h.discriminator:
            lines.append(f"      가르는 기준: {h.discriminator}  ({checkable})")
        else:
            lines.append(f"      가르는 기준을 찾지 못함 — 진짜 혼재로 보임")
        if h.note:
            lines.append(f"      {h.note}")
    lines.append(f"  전문: {root.name}/config.yml 의 split_hypotheses")
    return "\n".join(lines)


def run_learn(argv: list[str]) -> int:
    args = build_learn_parser().parse_args(argv)
    setup_logging(args.verbose)

    # Imported lazily, like the review pipeline's LLM stage, so the review
    # path never pays for pyyaml/provider-SDK imports it doesn't use.
    from pumpkins.conventions import (
        ConventionLearner,
        RuleScope,
        apply,
        extract_stats,
        load_all,
        reconcile,
        render_stats_yaml,
        select_files,
        write_config,
    )

    # The scan's reach becomes the scope of every rule it produces — a rule is
    # only trustworthy over the code it was measured on.
    scan_scope = RuleScope(
        paths=args.include or [], exclude_paths=args.exclude or []
    )

    try:
        scanned = len(
            select_files(args.repo, args.include, args.exclude, args.include_tests)
        )
        stats = extract_stats(
            args.repo, args.include, args.exclude, args.include_tests
        )
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

        # Naming a hidden split is inference, not the structured classification
        # the learn tier was chosen for (design doc §4). Measured on fmt: the
        # cheap tier saw the directories and still answered "no structural
        # distinction"; the review tier read the same input and said
        # "test/gtest vs include/fmt". So say so rather than return a useless
        # hypothesis — the cost decision is the user's.
        if _has_split_signal(stats) and args.model == default_learn_model():
            log.warning(
                "숨은 쪼개짐이 감지됐습니다 — 이 판단은 learn 기본 모델(%s)로는 "
                "대개 실패합니다. `--model %s`로 다시 돌리면 가르는 기준을 얻을 수 "
                "있습니다.",
                args.model, default_review_model(),
            )

        learner = ConventionLearner(model=args.model)
        result = learner.learn(stats, scan_scope)

        # Merge into decisions already on disk rather than overwriting them.
        # Re-running learn must never cost the user their curation.
        root = args.out or (args.repo / CONVENTIONS_DIRNAME)
        rec = reconcile(
            load_all(root), result.rules, model=args.model, reconsider=args.reconsider
        )
        written = apply(root, rec, accept_all=args.accept_all)
        write_config(
            root, args.repo, args.model, stats, scan_scope, scanned,
            rejected=[r.model_dump() for r in result.rejected],
            split_hypotheses=[s.model_dump() for s in result.split_hypotheses],
        )
        print(_format_reconciliation(root, rec, written, args.accept_all))
        if result.split_hypotheses:
            print(_format_split_hypotheses(result.split_hypotheses, root))
    except Exception as exc:  # surface a clean error instead of a traceback wall
        log.error("%s", exc, exc_info=args.verbose)
        return 1
    return 0


def _force_utf8_streams() -> None:
    """Make stdout/stderr UTF-8 regardless of the console's locale encoding.

    On non-UTF-8 consoles (e.g. cp949 on Korean Windows) argparse's --help text
    and log records containing characters like the em dash (—) raise
    UnicodeEncodeError or render as mojibake. Reconfiguring the streams up front
    makes the CLI behave the same everywhere.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # e.g. a capture buffer under pytest — leave it alone
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass  # detached/closed stream — nothing we can do, don't crash


def main(argv: list[str] | None = None) -> int:
    _force_utf8_streams()
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
        result, context = run_pipeline(args)
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

    if args.out_dir:
        dump_run(args.out_dir, args.repo, result, report, context)
    return 0


if __name__ == "__main__":
    sys.exit(main())
