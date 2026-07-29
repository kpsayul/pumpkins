"""Everything pumpkins knows about C++ *syntax*, in one place.

This module answers "what does a declaration look like"; the convention layer
(conventions/extractor.py) answers "what do these names have in common". The two
were tangled together, which mattered the moment a second question came up —
access specifiers — because the knowledge needed to answer it (where does a
class body start, is this `class` or `struct`) was spread across two packages.

Deliberately a heuristic regex scan, not a parser: macros, exotic templates and
multi-line declarations slip through. That is acceptable because we measure
*dominant* conventions and the threshold gate absorbs the noise. tree-sitter is
the upgrade path, and this module is the seam it would replace.

Nothing here is a language abstraction. There is one language, and inventing a
plugin interface for it would be guessing at what the second one needs. What
this does buy is that adding the second language becomes a refactor with a
visible boundary instead of a hunt through two packages.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator, Literal

log = logging.getLogger(__name__)

NAME = "cpp"

# Extensions treated as C++ translation units / headers. A repo can add to or
# subtract from this — see languages/config.py — because the same suffix means
# different things in different projects.
EXTENSIONS = frozenset(
    {".cpp", ".cc", ".cxx", ".c++", ".h", ".hpp", ".hh", ".hxx", ".inl"}
)
# Compilable on their own. Headers are not, which is why clang-tidy skips them
# in shallow mode.
TU_EXTENSIONS = frozenset({".cpp", ".cc", ".cxx", ".c++"})

Access = Literal["private", "public"]

# --------------------------------------------------------------- declarations

_IDENT = r"[A-Za-z_]\w*"

_CLASS_RE = re.compile(rf"\b(?:class|struct)\s+({_IDENT})")

# `private:` / `public:` / `protected:` — the label, not a use of the keyword.
ACCESS_RE = re.compile(r"^\s*(public|private|protected)\s*:")

# A class body defaults to private, a struct body to public. Getting this wrong
# would mis-bucket every member in the file, so it is read from the keyword
# rather than assumed.
_STRUCT_RE = re.compile(r"\bstruct\s+[A-Za-z_]")

# Names shaped like a macro (all caps). Real macros vastly outnumber genuine
# ALL_CAPS types/functions in C++, and a template-heavy repo drowns in them
# (fmt: FMT_API, FMT_CONSTEXPR20, …), so they are left out of the function and
# class_type statistics rather than skewing them.
_MACRO_LIKE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Leading declaration qualifiers, used to tell a compile-time constant from a
# mutable member. They are different categories with different conventions —
# lumping them together has cost a real rule in practice: `kFoo` constants in
# the member bucket dragged the dominant member prefix down and would have made
# every new constant a violation of "members use the m prefix".
_QUALIFIERS_RE = re.compile(
    r"^\s*((?:(?:static|mutable|const|constexpr|inline|volatile|thread_local)\s+)*)"
)

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

# `#include <a/b.h>` / `#include "a/b.h"` → the path between the brackets.
# Regex rather than AST on purpose: an include line is lexically unambiguous, and
# the layering check must keep working on machines where tree-sitter is broken —
# "which layer may depend on which" is too central a rule to lose to a native lib.
INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^">]+)[">]')

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


def is_constant_decl(line: str) -> bool:
    """`static constexpr T k = …` / `static const T k = …` — not `const T& ref`."""
    quals = set(_QUALIFIERS_RE.match(line).group(1).split())
    return "constexpr" in quals or {"static", "const"} <= quals


def default_access(line: str) -> Access:
    """A `struct` body starts public, a `class` body starts private."""
    return "public" if _STRUCT_RE.search(line) else "private"


def normalize_access(specifier: str) -> Access:
    """`protected` is folded into `private`.

    Both are internal to the type, and C++ projects that suffix private members
    do the same to protected ones. Keeping them apart would only split a
    denominator that is already the smaller of the two.
    """
    return "public" if specifier == "public" else "private"


# ------------------------------------------------------------- line utilities

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


def _is_declared_name(name: str) -> bool:
    """Reject keywords and macro-shaped names for the function/class categories."""
    return name not in _KEYWORDS and not _MACRO_LIKE_RE.match(name)


def match_identifiers(
    line: str, in_class_body: bool, access: Access | None = None
) -> list[tuple[str, str]]:
    """Regex-match identifier declarations on one sanitized line.

    Returns (category, name) pairs. `in_class_body` gates member matching —
    outside a class body the same shape is a local variable.

    `access` splits members by visibility, which is a real convention boundary:
    spdlog suffixes private members with `_` 98% of the time and public fields
    12% of the time, so measuring them together produced 72% and the rule was
    rejected. When access is unknown — a diff hunk that shows no specifier —
    the generic `member_variable` category is returned instead of a guess, and
    only rules written against that generic category will apply.
    """
    out: list[tuple[str, str]] = []
    stripped = line.strip()
    # Preprocessor lines declare macros, not members/functions/types. Their
    # names are ALL_CAPS by convention and would pollute every category.
    if not stripped or stripped.startswith("#"):
        return out

    if (
        in_class_body
        and "(" not in line
        and not stripped.startswith(_MEMBER_SKIP_PREFIXES)
    ):
        m = _MEMBER_RE.match(line)
        if m and m.group(1) not in _KEYWORDS:
            if is_constant_decl(line):
                category = "constant"
            elif access == "private":
                category = "private_member"
            elif access == "public":
                category = "public_field"
            else:
                category = "member_variable"  # visibility not visible here
            out.append((category, m.group(1)))

    line = strip_template_params(line)

    cm = _CLASS_RE.search(line)
    if cm and _is_declared_name(cm.group(1)):
        out.append(("class_type", cm.group(1)))

    fm = _FUNC_RE.match(line)
    if fm and _is_declared_name(fm.group(1)):
        out.append(("function", fm.group(1)))
    return out


# ----------------------------------------------------------------- file scan

def scan(text: str) -> Iterator[tuple[str, str]]:
    """Yield (category, identifier) pairs for one C++ file.

    Tracks brace depth to know when a class body is open, and the access
    specifier in effect inside it. Approximate by construction — strings and
    comments are stripped first, which is enough for counting conventions.
    """
    depth = 0
    class_body_depths: list[int] = []  # brace depth of each open class body
    access_stack: list[Access] = []     # access in effect in each open class body
    pending_class: Access | None = None  # saw `class X`, its `{` not yet
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

        specifier = ACCESS_RE.match(line)
        if specifier and access_stack:
            access_stack[-1] = normalize_access(specifier.group(1))

        in_class = bool(class_body_depths and depth == class_body_depths[-1])
        matches = match_identifiers(
            line, in_class, access_stack[-1] if in_class and access_stack else None
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
                access_stack.append(default_access(line))
            elif ";" not in stripped:
                pending_class = default_access(line)
        elif pending_class is not None and stripped.startswith("{"):
            class_body_depths.append(depth + 1)
            access_stack.append(pending_class)
            pending_class = None
        elif pending_class is not None and ";" in stripped:
            pending_class = None

        depth += opens - closes
        while class_body_depths and depth < class_body_depths[-1]:
            class_body_depths.pop()
            if access_stack:
                access_stack.pop()
