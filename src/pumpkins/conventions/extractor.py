"""Convention learning, stage L1 — mechanical identifier extraction (no LLM).

Turns a repository into naming *statistics*: per identifier category
(member_variable / function / class_type), how names are prefixed, suffixed
and cased. Only these statistics plus a small raw-name sample are sent to the
LLM in stage L2 (learner.py) — never whole files — which is what keeps the
token cost of `pumpkins learn` low (docs/convention-detection-design.md §2,
방안 A).

This is intentionally a heuristic regex scan, not a parser: macros, exotic
templates and multi-line declarations will slip through. That is acceptable —
we measure *dominant* conventions, and the threshold gate (config
MIN_RULE_OCCURRENCES / MIN_RULE_CONSISTENCY) absorbs the noise. The upgrade
path is tree-sitter.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel, Field

from pumpkins.config import LEARN_SKIP_DIRS, LEARN_TEST_DIRS
from pumpkins.conventions.scope import normalize_path, path_matches
from pumpkins.languages import cpp, cpp_extensions

log = logging.getLogger(__name__)

MAX_SAMPLES = 40      # raw names per category shown to the LLM
MAX_FACET_SAMPLES = 8  # names per facet value, so a split can be shown as two groups
MAX_FILES = 5000      # safety cap for huge repos

# Identifier categories, each with its own conventions and its own denominator.
#
# Members are split by visibility because that is a real convention boundary,
# not a nicety: spdlog suffixes private members with `_` 98% of the time and
# public fields 12% of the time, so measuring them together produced 72% and the
# threshold gate — correctly, given what it was shown — rejected the rule. Two
# clean rules had been averaged into one unusable number.
#
# Constants stay unified: they are named for their constness rather than their
# visibility, and splitting them further would push the denominator under
# MIN_RULE_OCCURRENCES in most repos.
CATEGORIES = (
    "private_member",
    "public_field",
    "constant",
    "function",
    "class_type",
)

# C++ syntax lives in languages/cpp.py; re-exported here because checker.py and
# the tests speak this vocabulary. This module's own job is statistics.
sanitize_line = cpp.sanitize_line
strip_template_params = cpp.strip_template_params
match_identifiers = cpp.match_identifiers


class CategoryStats(BaseModel):
    """Naming statistics for one identifier category — the LLM's entire input.

    `total` is the denominator for prefix/suffix. Casing has its own smaller
    denominator (`casing_informative`) because single-word lowercase names
    carry no casing signal — see AMBIGUOUS_CASING.
    """

    category: str
    total: int = 0
    prefix_counts: dict[str, int] = Field(default_factory=dict)
    suffix_counts: dict[str, int] = Field(default_factory=dict)
    casing_counts: dict[str, int] = Field(default_factory=dict)
    casing_informative: int = 0  # names counted in casing_counts
    casing_ambiguous: int = 0    # names excluded from it (no casing signal)
    samples: list[str] = Field(default_factory=list)
    # Names grouped by facet value, keyed "suffix=_". Needed because a split can
    # only be explained by seeing the two groups side by side: statistics say
    # "63% vs 36%", the names say "these are googletest, those are ours".
    facet_samples: dict[str, list[str]] = Field(default_factory=dict)
    # Where each group's names came from, same keys. This is usually the actual
    # discriminator and it is invisible in a name: fmt's UpperCamel functions
    # are all in test/gtest/, a bundled framework with its own style. Without
    # this the model can only guess, and it did — "no structural distinction".
    facet_dirs: dict[str, list[str]] = Field(default_factory=dict)


# --------------------------------------------------------------- name facets

# A single lowercase word (`dump`, `value`, `data_`) satisfies lowerCamel and
# lower_snake equally — it has no word boundary to reveal which one the project
# follows. Counting it as a style of its own splits one real convention across
# three buckets and hides it from the threshold gate: fmt's members are
# uniformly snake_case, yet came out as single_lower 66% / lower_snake 33%,
# below MIN_RULE_CONSISTENCY. So these names are excluded from the casing
# statistics, and on the review side they never violate a casing rule.
AMBIGUOUS_CASING = "single_lower"

_CASING_COMPATIBLE = {AMBIGUOUS_CASING: frozenset({"lowerCamel", "lower_snake"})}


def casing_matches(observed: str, expected: str) -> bool:
    """Whether an identifier's observed casing satisfies a rule's expected one."""
    return observed == expected or expected in _CASING_COMPATIBLE.get(observed, ())


# ----------------------------------------------------------- hidden splits

# A distribution that fails the consistency threshold can mean two things, and
# they call for opposite responses: genuinely mixed naming (reject and stay
# quiet) or *two groups with two different conventions* measured as one (find
# the boundary and you have two clean rules).
#
# The two look different in the numbers. Real mixture scatters; a hidden split
# is close to bimodal — two values covering nearly everything, with the smaller
# one too large to be noise. spdlog's members read 72% suffixed before
# visibility was tracked; split by access they were 100% and 87%.
#
# So this only *detects the shape*. Naming the boundary needs judgement about
# what the code means, which is the LLM's job (learner.py asks).
SPLIT_MIN_COMBINED = 0.90  # 두 값이 이만큼 덮어야 "쪼개진 것"으로 본다
SPLIT_MIN_SECOND = 0.15    # 작은 쪽이 이보다 작으면 잡음


def detect_split_signal(
    counts: dict[str, int], total: int, consistency_threshold: float
) -> tuple[tuple[str, int], tuple[str, int]] | None:
    """The two groups a category looks split into, or None.

    Returns None when the dominant value already passes the threshold (there is
    a rule, nothing to investigate) or when the distribution is scattered
    rather than split.
    """
    if total <= 0 or len(counts) < 2:
        return None
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    top, second = ranked[0], ranked[1]
    if top[1] / total >= consistency_threshold:
        return None  # 이미 규칙이 됨
    if (top[1] + second[1]) / total < SPLIT_MIN_COMBINED:
        return None  # 두 무리가 아니라 산개
    if second[1] / total < SPLIT_MIN_SECOND:
        return None  # 작은 쪽이 잡음 수준
    return top, second

def split_pattern(name: str) -> tuple[str, str, str]:
    """Decompose an identifier into (prefix, suffix, casing) facets.

    e.g. "m_maxCount" -> ("m_", "(none)", "lowerCamel")
         "queue_"     -> ("(none)", "_", "single_lower")
    """
    prefix, core = "(none)", name
    if core.startswith("m_"):
        prefix, core = "m_", core[2:]
    elif core.startswith("s_"):
        prefix, core = "s_", core[2:]
    elif core.startswith("g_"):
        prefix, core = "g_", core[2:]
    elif core.startswith("_"):
        prefix, core = "_", core.lstrip("_")
    # Underscore-less Hungarian prefixes. The uppercase requirement is what
    # keeps `max`/`mutex`/`kind` out: only `mItemCount`, `kMaxSize` match.
    elif re.match(r"[mksg][A-Z]", core):
        prefix, core = core[0], core[1:]

    suffix = "(none)"
    if core.endswith("_"):
        suffix, core = "_", core.rstrip("_")

    return prefix, suffix, _classify_casing(core)


def _classify_casing(core: str) -> str:
    if not core:
        return "other"
    if re.fullmatch(r"[a-z][a-z0-9]*", core):
        return AMBIGUOUS_CASING  # no word boundary → no casing signal
    if re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+", core):
        return "lower_snake"
    if re.fullmatch(r"[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+", core):
        return "lowerCamel"
    if re.fullmatch(r"[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)*", core):
        return "UpperCamel"
    if re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*", core):
        return "UPPER_SNAKE"
    return "other"


# ------------------------------------------------------------------ scanning

def _scan_file(path: Path) -> Iterator[tuple[str, str]]:
    """Yield (category, identifier) pairs from one file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.debug("skipping unreadable file %s: %s", path, exc)
        return
    yield from cpp.scan(text)


def select_files(
    repo: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    include_tests: bool = False,
) -> list[Path]:
    """The C++ files a learn scan will read, after directory and glob scoping.

    Kept separate from extract_stats so the CLI can report the scan's reach —
    "which files produced these rules" is what makes conventions.yml auditable.
    """
    skip_dirs = LEARN_SKIP_DIRS if include_tests else LEARN_SKIP_DIRS | LEARN_TEST_DIRS
    extensions = cpp_extensions(repo)
    files: list[Path] = []
    for p in sorted(repo.rglob("*")):
        if p.suffix.lower() not in extensions or not p.is_file():
            continue
        rel = p.relative_to(repo)
        if set(rel.parts[:-1]) & skip_dirs:
            continue
        rel_posix = normalize_path(str(rel))
        if exclude and path_matches(rel_posix, exclude):
            continue
        if include and not path_matches(rel_posix, include):
            continue
        files.append(p)
    return files


def extract_stats(
    repo: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    include_tests: bool = False,
) -> list[CategoryStats]:
    """Scan a repo and build per-category naming statistics."""
    counters: dict[str, dict[str, Counter]] = {
        cat: {"prefix": Counter(), "suffix": Counter(), "casing": Counter()}
        for cat in CATEGORIES
    }
    names: dict[str, dict[str, None]] = {cat: {} for cat in counters}  # ordered de-dup
    facet_names: dict[str, dict[str, dict[str, None]]] = {cat: {} for cat in counters}
    facet_dirs: dict[str, dict[str, Counter]] = {cat: {} for cat in counters}
    totals: Counter = Counter()
    ambiguous: Counter = Counter()  # names with no casing signal, per category
    per_dir: Counter = Counter()    # identifiers per directory, for the scan log

    files = select_files(repo, include, exclude, include_tests)
    if len(files) > MAX_FILES:
        log.warning("repo has %d C++ files — scanning first %d", len(files), MAX_FILES)
        files = files[:MAX_FILES]

    for path in files:
        directory = str(path.relative_to(repo).parent)
        for category, name in _scan_file(path):
            per_dir[directory] += 1
            prefix, suffix, casing = split_pattern(name)
            counters[category]["prefix"][prefix] += 1
            counters[category]["suffix"][suffix] += 1
            for facet, value in (("prefix", prefix), ("suffix", suffix), ("casing", casing)):
                key = f"{facet}={value}"
                bucket = facet_names[category].setdefault(key, {})
                if len(bucket) < MAX_FACET_SAMPLES:
                    bucket.setdefault(name)
                facet_dirs[category].setdefault(key, Counter())[directory] += 1
            if casing == AMBIGUOUS_CASING:
                ambiguous[category] += 1
            else:
                counters[category]["casing"][casing] += 1
            totals[category] += 1
            names[category].setdefault(name)

    stats = [
        CategoryStats(
            category=cat,
            total=totals[cat],
            prefix_counts=dict(counters[cat]["prefix"].most_common()),
            suffix_counts=dict(counters[cat]["suffix"].most_common()),
            casing_counts=dict(counters[cat]["casing"].most_common()),
            casing_informative=totals[cat] - ambiguous[cat],
            casing_ambiguous=ambiguous[cat],
            samples=list(names[cat])[:MAX_SAMPLES],
            facet_samples={k: list(v) for k, v in facet_names[cat].items()},
            facet_dirs={
                k: [f"{d} ({n})" for d, n in c.most_common(3)]
                for k, c in facet_dirs[cat].items()
            },
        )
        for cat in counters
    ]
    log.info(
        "scanned %d file(s): %s",
        len(files),
        ", ".join(
            f"{s.category}={s.total}"
            + (f" ({s.casing_ambiguous} casing-ambiguous)" if s.casing_ambiguous else "")
            for s in stats
        ),
    )
    # Which directories the rules actually came from. A vendored dependency
    # under an unrecognized name shows up here as an outsized contributor —
    # that is how fmt's bundled test/gtest/ was caught.
    for directory, count in per_dir.most_common(8):
        log.info("  %6d identifier(s) from %s/", count, directory)
    return stats
