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
from dataclasses import dataclass
from typing import Iterator

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


# Nodes that can carry a function declarator + a return type.
_FUNCTIONISH = {"function_definition", "declaration", "field_declaration"}


def _text(node) -> str:
    return node.text.decode("utf-8", "replace")


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


def scan(source: str) -> Iterator[tuple[str, str]]:
    """Yield (category, identifier) for a C++ source — the naming extractor's input.

    Same contract as cpp_parser.scan (the regex scanner), but AST-accurate:
    template parameters are not classes, macros are not functions, and member
    visibility comes from the tree rather than a hunk heuristic. Empty when
    tree-sitter is unavailable — the caller can fall back to the regex scanner."""
    if _LANGUAGE is None:
        return
    tree = Parser(_LANGUAGE).parse(source.encode("utf-8", "replace"))
    yield from _iter_declarations(tree.root_node, None)


def functions(source: str) -> list[FunctionDecl]:
    """Every function/method declaration in a C++ source, with its return type.

    Returns [] when tree-sitter is unavailable — callers treat that as "cannot
    verify" rather than a parse failure."""
    if _LANGUAGE is None:
        return []
    tree = Parser(_LANGUAGE).parse(source.encode("utf-8", "replace"))
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
