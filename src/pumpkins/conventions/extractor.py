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

from pumpkins.config import CPP_EXTENSIONS, LEARN_SKIP_DIRS

log = logging.getLogger(__name__)

MAX_SAMPLES = 40      # raw names per category shown to the LLM
MAX_FILES = 5000      # safety cap for huge repos

_IDENT = r"[A-Za-z_]\w*"

_CLASS_RE = re.compile(rf"\b(?:class|struct)\s+({_IDENT})")

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
    """Naming statistics for one identifier category — the LLM's entire input."""

    category: str
    total: int = 0
    prefix_counts: dict[str, int] = Field(default_factory=dict)
    suffix_counts: dict[str, int] = Field(default_factory=dict)
    casing_counts: dict[str, int] = Field(default_factory=dict)
    samples: list[str] = Field(default_factory=list)


# --------------------------------------------------------------- name facets

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
    elif re.match(r"k[A-Z]", core):
        prefix, core = "k", core[1:]

    suffix = "(none)"
    if core.endswith("_"):
        suffix, core = "_", core.rstrip("_")

    return prefix, suffix, _classify_casing(core)


def _classify_casing(core: str) -> str:
    if not core:
        return "other"
    if re.fullmatch(r"[a-z][a-z0-9]*", core):
        return "single_lower"  # ambiguous between snake/camel — kept separate
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


def match_identifiers(line: str, member_context: bool) -> list[tuple[str, str]]:
    """Regex-match identifier declarations on one sanitized line.

    Returns (category, name) pairs. `member_context` gates member-variable
    matching — outside a class body the same shape is a local variable.
    Shared by the repo scanner below and the diff checker (checker.py).
    """
    out: list[tuple[str, str]] = []
    stripped = line.strip()
    if not stripped:
        return out

    if (
        member_context
        and "(" not in line
        and not stripped.startswith(_MEMBER_SKIP_PREFIXES)
    ):
        m = _MEMBER_RE.match(line)
        if m and m.group(1) not in _KEYWORDS:
            out.append(("member_variable", m.group(1)))

    cm = _CLASS_RE.search(line)
    if cm and cm.group(1) not in _KEYWORDS:
        out.append(("class_type", cm.group(1)))

    fm = _FUNC_RE.match(line)
    if fm and fm.group(1) not in _KEYWORDS:
        out.append(("function", fm.group(1)))
    return out


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


def extract_stats(repo: Path) -> list[CategoryStats]:
    """Scan a repo and build per-category naming statistics."""
    counters: dict[str, dict[str, Counter]] = {
        cat: {"prefix": Counter(), "suffix": Counter(), "casing": Counter()}
        for cat in ("member_variable", "function", "class_type")
    }
    names: dict[str, dict[str, None]] = {cat: {} for cat in counters}  # ordered de-dup
    totals: Counter = Counter()

    files = [
        p for p in sorted(repo.rglob("*"))
        if p.suffix in CPP_EXTENSIONS
        and p.is_file()
        and not (set(p.relative_to(repo).parts[:-1]) & LEARN_SKIP_DIRS)
    ]
    if len(files) > MAX_FILES:
        log.warning("repo has %d C++ files — scanning first %d", len(files), MAX_FILES)
        files = files[:MAX_FILES]

    for path in files:
        for category, name in _scan_file(path):
            prefix, suffix, casing = split_pattern(name)
            counters[category]["prefix"][prefix] += 1
            counters[category]["suffix"][suffix] += 1
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
            samples=list(names[cat])[:MAX_SAMPLES],
        )
        for cat in counters
    ]
    log.info(
        "scanned %d file(s): %s",
        len(files),
        ", ".join(f"{s.category}={s.total}" for s in stats),
    )
    return stats
