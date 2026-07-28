"""C++ AST via tree-sitter — structural facts the regex parser cannot reach.

[cpp_parser.py](cpp_parser.py) answers "what does a declaration look like" with
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


@dataclass(frozen=True)
class FunctionDecl:
    """A function/method declaration or definition, and its declared return type.

    `return_type` is the text of the type node (e.g. "std::unique_ptr<Widget>");
    a raw-pointer return like `Widget*` shows the pointer in the declarator, not
    here, which is exactly the distinction an ownership convention cares about.
    """

    name: str
    return_type: str


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
                    out.append(FunctionDecl(name, _return_type(node, func_decl)))
        stack.extend(node.children)
    return out
