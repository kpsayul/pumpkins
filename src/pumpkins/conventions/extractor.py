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

from pumpkins.config import CPP_EXTENSIONS, LEARN_SKIP_DIRS, LEARN_TEST_DIRS
from pumpkins.conventions.scope import normalize_path, path_matches

log = logging.getLogger(__name__)

MAX_SAMPLES = 40      # raw names per category shown to the LLM
MAX_FILES = 5000      # safety cap for huge repos

# Identifier categories. Constants are tracked apart from mutable members
# because C++ projects almost always name them differently (`kMaxSize` vs
# `mCount`, `MAX_SIZE` vs `count_`).
CATEGORIES = ("member_variable", "constant", "function", "class_type")

_IDENT = r"[A-Za-z_]\w*"

_CLASS_RE = re.compile(rf"\b(?:class|struct)\s+({_IDENT})")

# Names shaped like a macro (all caps). Real macros vastly outnumber genuine
# ALL_CAPS types/functions in C++, and a template-heavy repo drowns in them
# (fmt: FMT_API, FMT_CONSTEXPR20, …), so they are left out of the function and
# class_type statistics rather than skewing them.
_MACRO_LIKE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Leading declaration qualifiers, used to tell a compile-time constant from a
# mutable member. They are different categories with different conventions —
# lumping them together has cost a real rule in practice: `kFoo` constants in
# the member_variable bucket dragged the dominant member prefix down and would
# have made every new constant a violation of "members use the m prefix".
_QUALIFIERS_RE = re.compile(
    r"^\s*((?:(?:static|mutable|const|constexpr|inline|volatile|thread_local)\s+)*)"
)


def _is_constant_decl(line: str) -> bool:
    """`static constexpr T k = …` / `static const T k = …` — not `const T& ref`."""
    quals = set(_QUALIFIERS_RE.match(line).group(1).split())
    return "constexpr" in quals or {"static", "const"} <= quals

# Function decl/def at statement start: requires a type-ish token *before* the
# name, so plain calls (`foo(x);`, `obj.foo(x)`) don't match.
_FUNC_RE = re.compile(
    rf"^\s*(?:(?:virtual|static|inline|explicit|constexpr|friend|extern)\s+)*"
    rf"{_IDENT}[\w:<>,*&\s]*?[\s*&]"
    rf"(?:{_IDENT}::)*({_IDENT})\s*\("
)

# Member variable declaration inside a class body (line without parentheses).
_MEMBER_RE = re.compile(
    rf"^\s*(?:(?:static|mutable|const|constexpr|inline|volatile)\s+)*"
    rf"{_IDENT}[\w:<>,*&\s]*?[\s*&]"
    rf"({_IDENT})\s*(?:=[^;{{]*|\{{[^;}}]*\}})?;"
)

_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"' + r"|'(?:\\.|[^'\\])*'")

_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "sizeof", "new",
    "delete", "throw", "case", "default", "else", "do", "operator",
    "static_assert", "alignof", "decltype", "const", "auto", "void", "int",
    "bool", "char", "float", "double", "long", "short", "unsigned", "signed",
    "true", "false", "nullptr", "this", "namespace", "template", "typename",
    "using", "typedef", "enum", "class", "struct", "union", "public",
    "private", "protected", "friend", "override", "final", "noexcept",
}

_MEMBER_SKIP_PREFIXES = (
    "public", "private", "protected", "using", "typedef", "friend",
    "template", "namespace", "return", "#", "enum", "class", "struct",
)


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

def sanitize_line(line: str) -> str:
    """Strip line comments and string/char literals (heuristic, single line)."""
    line = line.split("//", 1)[0]
    return _STRING_RE.sub('""', line)


def strip_template_params(line: str) -> str:
    """Remove `template <...>` parameter clauses from a line.

    The `class T` / `struct U` spellings inside them otherwise register as class
    declarations, so in a template-heavy repo the parameter names take over the
    class_type statistics (fmt: T, Char, OutputIt made UpperCamel look like 41%
    of a codebase that names its types in snake_case).
    """
    out = line
    while True:
        m = re.search(r"\btemplate\s*<", out)
        if m is None:
            return out
        depth, end = 0, None
        for j in range(m.end() - 1, len(out)):
            if out[j] == "<":
                depth += 1
            elif out[j] == ">":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end is None:
            return out[: m.start()]  # clause continues on the next line
        out = out[: m.start()] + " " + out[end + 1 :]


def match_identifiers(line: str, member_context: bool) -> list[tuple[str, str]]:
    """Regex-match identifier declarations on one sanitized line.

    Returns (category, name) pairs. `member_context` gates member-variable
    matching — outside a class body the same shape is a local variable.
    Shared by the repo scanner below and the diff checker (checker.py).
    """
    out: list[tuple[str, str]] = []
    stripped = line.strip()
    # Preprocessor lines declare macros, not members/functions/types. Their
    # names are ALL_CAPS by convention and would pollute every category.
    if not stripped or stripped.startswith("#"):
        return out

    if (
        member_context
        and "(" not in line
        and not stripped.startswith(_MEMBER_SKIP_PREFIXES)
    ):
        m = _MEMBER_RE.match(line)
        if m and m.group(1) not in _KEYWORDS:
            category = "constant" if _is_constant_decl(line) else "member_variable"
            out.append((category, m.group(1)))

    line = strip_template_params(line)

    cm = _CLASS_RE.search(line)
    if cm and _is_declared_name(cm.group(1)):
        out.append(("class_type", cm.group(1)))

    fm = _FUNC_RE.match(line)
    if fm and _is_declared_name(fm.group(1)):
        out.append(("function", fm.group(1)))
    return out


def _is_declared_name(name: str) -> bool:
    """Reject keywords and macro-shaped names for the function/class categories."""
    return name not in _KEYWORDS and not _MACRO_LIKE_RE.match(name)


def _scan_file(path: Path) -> Iterator[tuple[str, str]]:
    """Yield (category, identifier) pairs from one C++ file. Heuristic."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.debug("skipping unreadable file %s: %s", path, exc)
        return

    depth = 0
    class_body_depths: list[int] = []  # brace depth of each open class body
    pending_class = False              # saw `class X` but its `{` not yet
    in_block_comment = False

    for raw in text.splitlines():
        line = raw
        if in_block_comment:
            if "*/" not in line:
                continue
            line = line.split("*/", 1)[1]
            in_block_comment = False
        if "/*" in line:
            head, _, tail = line.partition("/*")
            if "*/" in tail:
                line = head + tail.split("*/", 1)[1]
            else:
                line = head
                in_block_comment = True
        line = sanitize_line(line)
        stripped = line.strip()
        if not stripped:
            continue

        matches = match_identifiers(
            line,
            member_context=bool(class_body_depths and depth == class_body_depths[-1]),
        )
        class_here = False
        for category, name in matches:
            yield category, name
            if category == "class_type":
                class_here = True

        # brace bookkeeping (approximate — strings/comments already stripped)
        opens, closes = line.count("{"), line.count("}")
        if class_here:
            if "{" in line:
                class_body_depths.append(depth + 1)
            elif ";" not in stripped:
                pending_class = True
        elif pending_class and stripped.startswith("{"):
            class_body_depths.append(depth + 1)
            pending_class = False
        elif pending_class and ";" in stripped:
            pending_class = False

        depth += opens - closes
        while class_body_depths and depth < class_body_depths[-1]:
            class_body_depths.pop()


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
    files: list[Path] = []
    for p in sorted(repo.rglob("*")):
        if p.suffix not in CPP_EXTENSIONS or not p.is_file():
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
    totals: Counter = Counter()
    ambiguous: Counter = Counter()  # names with no casing signal, per category
    per_dir: Counter = Counter()    # identifiers per directory, for the scan log

    files = select_files(repo, include, exclude, include_tests)
    if len(files) > MAX_FILES:
        log.warning("repo has %d C++ files — scanning first %d", len(files), MAX_FILES)
        files = files[:MAX_FILES]

    for path in files:
        for category, name in _scan_file(path):
            per_dir[str(path.relative_to(repo).parent)] += 1
            prefix, suffix, casing = split_pattern(name)
            counters[category]["prefix"][prefix] += 1
            counters[category]["suffix"][suffix] += 1
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
