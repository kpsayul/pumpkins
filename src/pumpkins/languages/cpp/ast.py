"""C++ AST via tree-sitter — structural facts the regex parser cannot reach.

[parser.py](parser.py) answers "what does a declaration look like" with
regex: enough for naming statistics, blind to structure. Return types, ownership
(raw vs smart pointer), inheritance and the include graph are relationships a
regex cannot follow — you need a real parse tree. This module provides that.

tree-sitter is a normal dependency, but a **native** one: the `tree-sitter` core
and the `tree-sitter-cpp` grammar must be ABI-compatible, and a skew (or a
platform with no prebuilt wheel) breaks the import. Since this module is pulled
in transitively by the review and naming-learn paths — which do not need it —
the import is guarded so such a break degrades only structural checks (`verify`
returns None, the inferred rule stays an unverified guess) instead of taking
down unrelated commands. The guard isolates a fragile native lib; it is not a
user opt-out.

This is deliberately a thin extraction layer, not a query DSL: tree-sitter's
Python query API churns between releases, so we walk the tree with the stable
node API (`type`, `child_by_field_name`, `text`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, Iterator

from pumpkins.languages.cpp import parser as cpp_parser

log = logging.getLogger(__name__)

try:
    import tree_sitter_cpp
    from tree_sitter import Language, Parser

    _LANGUAGE = Language(tree_sitter_cpp.language())
    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # not installed, or an ABI mismatch between the two pkgs
    _LANGUAGE = None
    _IMPORT_ERROR = exc


def available() -> bool:
    """Whether structural (AST) extraction can run in this environment."""
    return _LANGUAGE is not None


def unavailable_reason() -> str:
    return f"tree-sitter not available ({_IMPORT_ERROR})" if _IMPORT_ERROR else ""


_warned: set[str] = set()


def require(purpose: str) -> bool:
    """Whether AST work can run — and say so, once, when it cannot.

    Falling back is not free: the regex scanner reads templates, macros and
    multi-line declarations differently, so the *statistics change* and with
    them the rules a repo learns. A degradation that changes results and says
    nothing is the same failure the review report was fixed to avoid — silence
    that reads as success.
    """
    if _LANGUAGE is not None:
        return True
    if purpose not in _warned:
        _warned.add(purpose)
        log.warning(
            "%s: %s — falling back to the regex scanner. Results will differ "
            "from a machine with tree-sitter installed. Fix with: "
            "pip install --force-reinstall 'tree-sitter>=0.23,<0.27' 'tree-sitter-cpp>=0.23,<0.24'",
            purpose, unavailable_reason(),
        )
    return False


def engine() -> str:
    """Which scanner is in use — recorded in run provenance because it changes results."""
    return "tree-sitter" if _LANGUAGE is not None else "regex-fallback"


@dataclass(frozen=True)
class FunctionDecl:
    """A function/method declaration or definition, and its declared return type.

    `return_type` is the text of the type node (e.g. "std::unique_ptr<Widget>");
    a raw-pointer return like `Widget*` shows the pointer in the declarator, not
    here, which is exactly the distinction an ownership convention cares about.

    `line` is the 1-based line where the declaration starts, so the review side
    can tell which functions a diff actually touched.
    """

    name: str
    return_type: str
    line: int = 0


@dataclass(frozen=True)
class MemberDecl:
    """A data member of a class/struct, and how it holds what it points at.

    `owner` is the enclosing class, `line` the 1-based declaration line — the
    two things a review needs to say *where*. The pointer flags exist because
    ownership is the convention people actually argue about: `Widget* m_child`
    and `std::unique_ptr<Widget> m_child` are the same field to a naming rule
    and opposite decisions to a reviewer.
    """

    name: str
    type_text: str
    owner: str
    line: int = 0
    is_raw_pointer: bool = False
    is_smart_pointer: bool = False

    @property
    def holds_pointer(self) -> bool:
        """Whether this member is part of the ownership question at all.

        This is the denominator for an ownership rule. Members held by value are
        not a choice between raw and smart, so counting them would drag every
        measured coverage toward the share of plain `int` fields — the same
        mistake as measuring casing on single-word lowercase names.
        """
        return self.is_raw_pointer or self.is_smart_pointer


@dataclass(frozen=True)
class ClassDecl:
    """A class/struct declaration and the bases it derives from."""

    name: str
    bases: list[str]
    line: int = 0


# Types whose raw pointer is conventionally a view, not ownership. Counting
# `const char* m_name` as an ownership violation would reject true rules on the
# strength of C-string parameters, so they leave the denominator entirely.
_NON_OWNING_POINTEE = {
    "char", "wchar_t", "char8_t", "char16_t", "char32_t", "void", "FILE",
}
_SMART_POINTER_HINTS = (
    "unique_ptr", "shared_ptr", "weak_ptr", "scoped_ptr", "intrusive_ptr",
)

# Nodes that can carry a function declarator + a return type.
_FUNCTIONISH = {"function_definition", "declaration", "field_declaration"}


def _text(node) -> str:
    return node.text.decode("utf-8", "replace")


# ------------------------------------------- macros in class headers (recovery)
#
# tree-sitter has no preprocessor, so an export/visibility macro in a class
# header derails it:
#
#     class YAML_CPP_API Exception : public std::runtime_error { int m_x; };
#
# It reads the MACRO as the class name; the class is misnamed AND every member
# disappears (the body is taken for a function body, so members become locals).
# The pattern is near-universal in libraries that ship a DLL — FMT_API,
# SPDLOG_API, *_EXPORT — so the repos most likely to have conventions worth
# learning are exactly the ones whose statistics were quietly wrong.
#
# Three shapes, and they fail differently — which is why there is no single
# clever pattern that covers them:
#
#   class M Foo : Base { … }   → tree-sitter reports an ERROR node
#   class M Foo { … }          → NO error at all. Silently a function_definition
#   class M Foo;               → NO error, and INDISTINGUISHABLE from the valid
#                                C++ `class Foo bar;` (a variable of class type)
#
# The last one settles the design: the grammar is genuinely ambiguous here, so
# no amount of shape-matching on the text can resolve it. The only real signal is
# whether the token is a macro — which the repo itself states, in its `#define`s.
# So we ask the repo (`collect_macros`) rather than guessing from the name.
#
# For the first two shapes we do not need to guess either: the parse tree proves
# the misparse. `class M Foo { … }` becomes a function_definition whose declarator
# is a bare identifier, and a function definition cannot have one — a real one
# needs a parameter list. That is a proof, not a heuristic.

_CLASSISH = ("class_specifier", "struct_specifier")


def _blank_span(source: str, spans: list[tuple[int, int]]) -> str:
    """Replace byte spans with spaces, preserving newlines.

    Spaces rather than deletion so byte offsets and line numbers survive — a
    structural finding reports the line it was found on, and a shifted line
    number points the reviewer at the wrong code.
    """
    data = bytearray(source.encode("utf-8", "replace"))
    for start, end in spans:
        for i in range(start, min(end, len(data))):
            if data[i] != 0x0A:  # keep '\n'
                data[i] = 0x20
    return data.decode("utf-8", "replace")


def _macro_span(node, macros: MacroTable) -> tuple[int, int] | None:
    """Byte span to blank when this node is `class <macro> Name …`, else None.

    Accepted on either of two independent grounds:
      - the token is a macro the repo `#define`s (settles the ambiguous case), or
      - the tree proves a misparse (a function_definition whose declarator is a
        bare identifier — not expressible in valid C++).
    """
    if node.type not in ("function_definition", "declaration"):
        return None
    kids = node.children
    if not kids or kids[0].type not in _CLASSISH:
        return None
    spec = kids[0]
    if spec.child_by_field_name("body") is not None:
        return None  # a real class definition; nothing was misread
    name = spec.child_by_field_name("name")
    if name is None or len(kids) < 2:
        return None
    following = kids[1]
    known_macro = _text(name) in macros

    # Where the real class name sits depends on which way the parse went wrong:
    #   ERROR            `class M Foo : Base { … }`  — choked on the real name
    #   init_declarator  `class M Foo : Base {}`     — read as a variable + init
    #   identifier       `class M Foo { … }` / `class M Foo;`
    if following.type == "ERROR":
        real = next((c for c in following.children if c.type == "identifier"), None)
        return (name.start_byte, real.start_byte) if real is not None else None

    if following.type == "init_declarator":
        real = following.children[0] if following.children else None
        if real is None or real.type != "identifier":
            return None
        # A parse error inside the declarator proves this is not the variable
        # declaration it was read as; otherwise fall back to the macro list.
        if following.has_error or known_macro:
            return (name.start_byte, real.start_byte)
        return None

    if following.type == "identifier":
        # A function definition cannot have a bare identifier as its declarator —
        # a real one needs a parameter list. So this shape is not valid C++, which
        # makes it a proof rather than a guess.
        proven = node.type == "function_definition" and any(
            k.type == "compound_statement" for k in kids
        )
        if proven or known_macro:
            return (name.start_byte, following.start_byte)
    return None


def _recovery_spans(node, macros: MacroTable, out: list[tuple[int, int]]) -> None:
    span = _macro_span(node, macros)
    if span is not None:
        out.append(span)
    for child in node.children:
        _recovery_spans(child, macros, out)


def _error_count(node) -> int:
    if not node.has_error:
        return 0
    total = 1 if (node.type == "ERROR" or node.is_missing) else 0
    return total + sum(_error_count(c) for c in node.children)


@dataclass(frozen=True)
class ParseReport:
    """How well one source parsed — after recovery.

    Two numbers, because the failures that hurt most are the quiet ones.
    `error_nodes` is what the parser admits it could not read. `unresolved` is
    what it read *confidently and possibly wrongly*: a `class X Y` we could not
    settle, which is either an unknown macro (statistics silently wrong) or a
    genuine `class Foo bar;` declaration (fine). Either way it is a place where
    the tool is guessing, and a guess nobody counts is how 51 members went
    missing from a repo's statistics without anyone noticing.
    """

    error_nodes: int = 0
    unresolved: int = 0

    @property
    def clean(self) -> bool:
        return self.error_nodes == 0 and self.unresolved == 0


@dataclass
class ScanHealth:
    """Parse health across a whole scan, and the files that account for it.

    The two counts are kept apart because only one of them costs data, and
    conflating them makes the warning useless. Measured on yaml-cpp:

        unresolved class headers  25 → 0   (this is what lost 51 members)
        parse errors             337 → 235 (the rest is SFINAE templates)

    Those 235 are real limits of the grammar, sitting inside template parameter
    lists, and they cost nothing — every class in that file still came out
    correctly. Warning about them would fire on every template-heavy C++ repo
    forever, which trains people to ignore the one warning that matters.
    """

    files: int = 0
    error_nodes: int = 0
    files_with_errors: int = 0
    unresolved: int = 0
    files_with_unresolved: int = 0
    worst: list[tuple[str, int, int]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.worst is None:
            self.worst = []

    def add(self, path: str, report: ParseReport) -> None:
        self.files += 1
        self.error_nodes += report.error_nodes
        self.unresolved += report.unresolved
        self.files_with_errors += report.error_nodes > 0
        self.files_with_unresolved += report.unresolved > 0
        if not report.clean:
            self.worst.append((path, report.unresolved, report.error_nodes))

    def finalize(self, limit: int = 5) -> None:
        # Unresolved first: a file that lost a class matters more than one with
        # a hundred harmless template errors.
        self.worst = sorted(self.worst, key=lambda w: (-w[1], -w[2]))[:limit]

    @property
    def error_ratio(self) -> float:
        return self.files_with_errors / self.files if self.files else 0.0

    @property
    def needs_attention(self) -> bool:
        """Whether a human should look. Unresolved headers always qualify —
        each one is a class whose members may be missing from the statistics."""
        return self.unresolved > 0

    def summary(self) -> str:
        if self.unresolved:
            return (
                f"{self.files}개 파일 중 {self.files_with_unresolved}개에서 "
                f"class 선언 {self.unresolved}곳을 판정하지 못했습니다 "
                f"(이 클래스들의 멤버가 통계에서 빠졌을 수 있습니다)"
            )
        if self.error_nodes:
            return (
                f"{self.files}개 파일, class 선언은 모두 판정됨 · "
                f"파서가 못 읽은 구간 {self.error_nodes}곳 "
                f"({self.files_with_errors}개 파일, 주로 복잡한 템플릿 — 통계에는 영향 없음)"
            )
        return f"{self.files}개 파일 모두 정상 파싱"


def _unresolved_count(node) -> int:
    """`class X Y` sites recovery left alone — ambiguous, so possibly misread."""
    total = 0
    if node.type in ("function_definition", "declaration"):
        kids = node.children
        if (
            kids
            and kids[0].type in _CLASSISH
            and kids[0].child_by_field_name("body") is None
            and len(kids) > 1
            and kids[1].type in ("identifier", "ERROR", "init_declarator")
        ):
            total += 1
    return total + sum(_unresolved_count(c) for c in node.children)


class MacroTable:
    """The object-like macros a repo defines — the closest thing we get to a
    preprocessor, built entirely from the repo's own `#define` lines.

    `expand` substitutes them throughout a source before parsing. Doing it
    everywhere rather than only in class headers is not a stylistic preference;
    it is measurably more correct. On yaml-cpp, expanding the whole file dropped
    parse errors 235 → 76 and cleaned up statistics that position-limited
    recovery left broken:

        function 'string FpToString'      → function 'FpToString'
        function 'vector<Node> LoadAll'   → function 'LoadAll'
        public_field 'override'  (×14)    → gone (it is a keyword)
        public_field 'JKJ_CONSTEXPR14'    → gone (it is a macro name)

    Preprocessor directive lines are left alone. Every header opens with
    `#ifndef GUARD` / `#define GUARD`, and expanding the guard to nothing leaves
    a bare `#ifndef` — one fresh error in 49 of 97 files when tried.
    """

    # Bounded passes, because a macro body may name another macro
    # (`#define A  B C`). A fixed small number beats a fixpoint loop: it cannot
    # spin on a recursive definition, and the error-count guard decides anyway.
    MAX_PASSES = 3

    def __init__(self, bodies):
        if not isinstance(bodies, dict):  # an iterable of bare names
            bodies = {name: "" for name in bodies}
        self._bodies: dict[str, str] = bodies
        self._use = (
            re.compile(
                r"\b(" + "|".join(sorted(map(re.escape, bodies), key=len, reverse=True)) + r")\b"
            )
            if bodies
            else None
        )

    def __contains__(self, name: str) -> bool:
        return name in self._bodies

    def __bool__(self) -> bool:
        return bool(self._bodies)

    def __len__(self) -> int:
        return len(self._bodies)

    def expand(self, source: str) -> str:
        """Substitute known macros outside preprocessor lines.

        Line count never changes — bodies are single-line by construction — so
        every reported line number still points at the original code.
        """
        if self._use is None:
            return source
        text = source
        for _ in range(self.MAX_PASSES):
            replaced = "".join(
                line
                if line.lstrip().startswith("#")
                else self._use.sub(lambda m: self._bodies[m.group(1)], line)
                for line in text.splitlines(keepends=True)
            )
            if replaced == text:
                break
            text = replaced
        return text


NO_MACROS = MacroTable({})


def collect_macros(texts: Iterable[str]) -> MacroTable:
    """The object-like macros the given sources define, with their bodies.

    A fact about this repo, read from this repo — not a pattern someone
    hardcoded. Same attitude the rule side takes: do not guess, ask the code.
    """
    bodies: dict[str, str] = {}
    for text in texts:
        for name, body in cpp_parser.object_like_macros(text).items():
            bodies.setdefault(name, body)
    return MacroTable(bodies)


def _parse(source: str, macros: MacroTable = NO_MACROS):
    """Parse tree for a C++ source, after doing what we can about macros.

    Two mechanisms, in order, each accepted only if it does not increase the
    error count. That guard is what allows running both on every file with no
    per-repo allowlist: recovery can never make a parse worse than leaving it
    alone.

    1. **Expand** the repo's own macros (MacroTable). Handles anything the repo
       defines, anywhere in the file.
    2. **Recover** a macro-derailed class header from the parse tree itself.
       Still needed after (1) for macros the repo does *not* define — supplied by
       the compiler, or by a third-party header the scan skipped — where the tree
       proves the misparse without anyone knowing the name.
    """
    if _LANGUAGE is None:
        return None
    parser = Parser(_LANGUAGE)
    best = parser.parse(source.encode("utf-8", "replace"))

    if macros:
        expanded = macros.expand(source)
        if expanded != source:
            candidate = parser.parse(expanded.encode("utf-8", "replace"))
            if _error_count(candidate.root_node) <= _error_count(best.root_node):
                best, source = candidate, expanded

    spans: list[tuple[int, int]] = []
    _recovery_spans(best.root_node, macros, spans)
    if spans:
        candidate = parser.parse(_blank_span(source, spans).encode("utf-8", "replace"))
        if _error_count(candidate.root_node) <= _error_count(best.root_node):
            best = candidate
    return best


def _function_declarator(node):
    """Descend a declarator chain (pointer/reference/…) to its function_declarator."""
    while node is not None:
        if node.type == "function_declarator":
            return node
        node = node.child_by_field_name("declarator")
    return None


def _declared_name(func_declarator) -> str:
    d = func_declarator.child_by_field_name("declarator")
    if d is None:
        return ""
    if d.type == "qualified_identifier":  # Foo::bar → bar
        name = d.child_by_field_name("name")
        return _text(name) if name is not None else _text(d).split("::")[-1]
    return _text(d)  # identifier / field_identifier / operator_name / …


def _return_type(node, func_declarator) -> str:
    """The declared return type. A trailing return type (`auto f() -> T`) wins
    over the `auto` placeholder — common in modern C++, and the whole point of an
    ownership/factory check is the real T, not `auto`."""
    for child in func_declarator.children:
        if child.type == "trailing_return_type":
            for part in child.children:
                if part.type == "type_descriptor":
                    return _text(part)
            return _text(child).lstrip("-> ").strip()
    type_node = node.child_by_field_name("type")
    return _text(type_node) if type_node is not None else ""


# ---------------------------------------------------------- identifier scan

# Same categories the regex scanner (cpp_parser.scan) yields, so the naming
# extractor can swap parsers without changing its statistics contract.
_CATEGORIES = ("private_member", "public_field", "constant", "function", "class_type")


def _norm_access(text: str) -> str:
    return "public" if text.strip() == "public" else "private"  # protected folds into private


def _is_constant_field(node) -> bool:
    """`static constexpr` / `static const` — a compile-time constant, named apart
    from a mutable member. `const T&` (no static) is a member, not a constant."""
    quals = {
        c.text.decode() for c in node.children
        if c.type in ("storage_class_specifier", "type_qualifier")
    }
    return "constexpr" in quals or {"static", "const"} <= quals


def _plain_named_function(node, func_declarator) -> bool:
    """Exclude constructors/destructors/operators: a naming stat about "functions"
    should not be polluted by names that are the class name or `operator=`."""
    inner = func_declarator.child_by_field_name("declarator")
    if inner is not None and inner.type in ("operator_name", "destructor_name"):
        return False
    # constructors/destructors have no return type
    if node.child_by_field_name("type") is not None:
        return True
    return any(c.type == "trailing_return_type" for c in func_declarator.children)


def _variable_name(declarator) -> str:
    """Descend a member declarator (pointer/reference/array/init wrappers) to its
    field_identifier."""
    node = declarator
    while node is not None:
        if node.type in ("field_identifier", "identifier"):
            return _text(node)
        nxt = node.child_by_field_name("declarator")
        if nxt is None:
            nxt = next(
                (c for c in node.children
                 if c.type in ("field_identifier", "identifier", "reference_declarator",
                               "pointer_declarator", "array_declarator", "init_declarator")),
                None,
            )
        node = nxt
    return ""


def _classify_field(node, access: str) -> Iterator[tuple[str, str]]:
    """A class-body field_declaration → a member function, member var, or constant."""
    func_decl = _function_declarator(node.child_by_field_name("declarator"))
    if func_decl is not None:
        name = _declared_name(func_decl)
        if name and _plain_named_function(node, func_decl):
            yield ("function", name)
        return
    name = _variable_name(node.child_by_field_name("declarator"))
    if not name:
        return
    if _is_constant_field(node):
        yield ("constant", name)
    else:
        yield ("private_member" if access == "private" else "public_field", name)


def _iter_declarations(node, access: str | None) -> Iterator[tuple[str, str]]:
    kind = node.type
    if kind in ("class_specifier", "struct_specifier"):
        name = node.child_by_field_name("name")
        if name is not None and name.type == "type_identifier":
            yield ("class_type", _text(name))
        body = node.child_by_field_name("body")
        if body is not None:
            current = "public" if kind == "struct_specifier" else "private"
            for child in body.children:
                if child.type == "access_specifier":
                    current = _norm_access(_text(child))
                else:
                    yield from _iter_declarations(child, current)
        return

    if kind == "field_declaration" and access is not None:
        yield from _classify_field(node, access)
        return

    if kind in ("function_definition", "declaration"):
        func_decl = _function_declarator(node.child_by_field_name("declarator"))
        if func_decl is not None and access is None:  # a free (non-member) function
            name = _declared_name(func_decl)
            if name and _plain_named_function(node, func_decl):
                yield ("function", name)
        # recurse into bodies to catch nested types; locals fall through harmlessly

    for child in node.children:
        yield from _iter_declarations(child, access)


def scan_with_report(
    source: str, macros: MacroTable = NO_MACROS
) -> tuple[list[tuple[str, str]], ParseReport | None]:
    """`scan`, plus how well the file parsed — from a single parse.

    The learn scan uses this so measuring parse health costs nothing extra: the
    tree is already built, and re-parsing every file just to count errors would
    make the honest thing the expensive thing.
    """
    tree = _parse(source, macros)
    if tree is None:
        return [], None
    return (
        list(_iter_declarations(tree.root_node, None)),
        ParseReport(_error_count(tree.root_node), _unresolved_count(tree.root_node)),
    )


def scan(source: str, macros: MacroTable = NO_MACROS) -> Iterator[tuple[str, str]]:
    """Yield (category, identifier) for a C++ source — the naming extractor's input.

    Same contract as cpp_parser.scan (the regex scanner), but AST-accurate:
    template parameters are not classes, macros are not functions, and member
    visibility comes from the tree rather than a hunk heuristic. Empty when
    tree-sitter is unavailable — the caller can fall back to the regex scanner."""
    tree = _parse(source, macros)
    if tree is None:
        return
    yield from _iter_declarations(tree.root_node, None)


# ------------------------------------------------------- members / ownership

def _contains_pointer_declarator(node) -> bool:
    """Whether a declarator chain declares a pointer.

    A reference (`T& m_ref`) stops the search: a reference member never owns,
    so it is neither side of the ownership question and must not be counted as
    a raw pointer."""
    if node is None:
        return False
    if node.type == "pointer_declarator":
        return True
    if node.type == "reference_declarator":
        return False
    return any(_contains_pointer_declarator(c) for c in node.children)


def _pointee_is_ownable(type_text: str) -> bool:
    base = type_text.replace("const", " ").replace("volatile", " ").strip()
    base = base.split("<")[0].split("::")[-1].strip()
    return base not in _NON_OWNING_POINTEE


def _walk_members(node, owner: str, out: list[MemberDecl]) -> None:
    kind = node.type
    if kind in ("class_specifier", "struct_specifier"):
        name_node = node.child_by_field_name("name")
        inner = _text(name_node) if name_node is not None else owner
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                _walk_members(child, inner, out)
        return

    if kind == "field_declaration" and owner:
        declarator = node.child_by_field_name("declarator")
        if _function_declarator(declarator) is None:  # a data member, not a method
            name = _variable_name(declarator)
            if name:
                type_node = node.child_by_field_name("type")
                type_text = _text(type_node) if type_node is not None else ""
                raw = _contains_pointer_declarator(declarator) and _pointee_is_ownable(type_text)
                out.append(
                    MemberDecl(
                        name=name,
                        type_text=type_text,
                        owner=owner,
                        line=node.start_point[0] + 1,
                        is_raw_pointer=raw,
                        is_smart_pointer=any(h in type_text for h in _SMART_POINTER_HINTS),
                    )
                )
        return

    for child in node.children:
        _walk_members(child, owner, out)


def members(source: str, macros: MacroTable = NO_MACROS) -> list[MemberDecl]:
    """Every data member declared in a C++ source, with its type and ownership shape.

    Returns [] when tree-sitter is unavailable — callers treat that as "cannot
    verify", never as "no violations"."""
    tree = _parse(source, macros)
    if tree is None:
        return []
    out: list[MemberDecl] = []
    _walk_members(tree.root_node, "", out)
    return out


def classes(source: str, macros: MacroTable = NO_MACROS) -> list[ClassDecl]:
    """Every class/struct with the bases it derives from — the inheritance edge.

    Feeds the structure survey and the hierarchy check: "all X derive from Y" is
    visible here long before any function body is read."""
    tree = _parse(source, macros)
    if tree is None:
        return []
    out: list[ClassDecl] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in ("class_specifier", "struct_specifier"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                bases: list[str] = []
                for child in node.children:
                    if child.type == "base_class_clause":
                        bases = [
                            _text(c) for c in child.children
                            if c.type in ("type_identifier", "qualified_identifier")
                        ]
                out.append(ClassDecl(_text(name_node), bases, node.start_point[0] + 1))
        stack.extend(node.children)
    return out


def functions(source: str, macros: MacroTable = NO_MACROS) -> list[FunctionDecl]:
    """Every function/method declaration in a C++ source, with its return type.

    Returns [] when tree-sitter is unavailable — callers treat that as "cannot
    verify" rather than a parse failure."""
    tree = _parse(source, macros)
    if tree is None:
        return []
    out: list[FunctionDecl] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in _FUNCTIONISH:
            func_decl = _function_declarator(node.child_by_field_name("declarator"))
            if func_decl is not None:
                name = _declared_name(func_decl)
                if name:
                    out.append(
                        FunctionDecl(name, _return_type(node, func_decl), node.start_point[0] + 1)
                    )
        stack.extend(node.children)
    return out
