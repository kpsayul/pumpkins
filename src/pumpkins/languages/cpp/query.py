"""tree-sitter queries — the one place the churning query API is absorbed.

Why this module exists at all
----------------------------
Every mechanical check used to be a hand-written branch: `naming`, `return_type`,
`member_ownership`, `base_class`, … Each one is ~20 lines of Python that only I
can add, so the set of conventions the tool can *measure* was a list I typed.
Inference kept proposing rules outside it — "접근자는 const를 붙인다",
"재정의엔 override를 쓴다", "클래스는 이 네임스페이스 안에 둔다" — and every one
died as "cannot verify" even though the model had read the code correctly and was
right. `base_class` exists only because I eventually hand-wrote it.

A query replaces the branch. The model writes what to look for, we run it. The
vocabulary stops being a list and becomes a language.

Two queries, not one
--------------------
A check is a pair, because coverage is a fraction and a fraction needs a
denominator that belongs to the rule:

    population  — the sites the rule is *about*      → the denominator
    conforming  — the sites that *satisfy* it        → the numerator

Both must capture the node under judgement as `@subject`; the fraction is
|conforming ∩ population| / |population|, matched by byte span on one parse tree.
Making the denominator a required field is deliberate. Every measurement bug this
project has paid for was a denominator quietly wider than the claim — casing
measured over names carrying no casing signal, ownership over members holding no
pointer, layering over a whole repo instead of one layer. A model that must state
the population cannot skip that decision.

Safety
------
A query only *matches*; it cannot call, assign or import. Running one the model
wrote is reading, not executing. A malformed query fails to compile and the rule
stays unverified — the same safe state as before. A well-formed but wrong query
produces a real number and the coverage gate rejects it. So a bad query can yield
a rejected candidate or an unmeasurable one, never an enforced wrong rule.

Version churn
-------------
This API moves between releases: `Query(lang, src)` vs `lang.query(src)`,
`QueryCursor(q).captures(node)` vs `q.captures(node)`, and `captures()` returning
a dict in newer versions but a list of `(node, name)` pairs in older ones. That
churn is why the rest of the codebase walks the tree by hand. It is contained
here so a version bump breaks one file instead of every check.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# The capture every check query must define. One fixed name keeps the contract
# small enough to state in a prompt.
SUBJECT = "subject"

try:
    import tree_sitter_cpp
    from tree_sitter import Language, Query

    _LANGUAGE = Language(tree_sitter_cpp.language())
    _ERROR: Exception | None = None
except Exception as exc:  # not installed, or an ABI skew between the two packages
    _LANGUAGE = None
    _ERROR = exc

try:  # tree-sitter >= 0.25 runs queries through a cursor
    from tree_sitter import QueryCursor

    def _run(query, node):
        return QueryCursor(query).captures(node)

except ImportError:  # older releases query the object directly

    def _run(query, node):
        return query.captures(node)


def available() -> bool:
    return _LANGUAGE is not None


@dataclass(frozen=True)
class Subject:
    """One node a query put under judgement, and where it is.

    `span` identifies the node so the two queries of a check can be intersected;
    `line` and `text` are what a review finding needs to point at it.
    """

    span: tuple[int, int]
    line: int
    text: str


class CompiledQuery:
    """A validated query, ready to run against parse trees."""

    def __init__(self, query, source: str):
        self._query = query
        self.source = source

    def subjects(self, tree) -> dict[tuple[int, int], Subject]:
        """The `@subject` nodes this query matches, keyed by byte span."""
        try:
            captures = _run(self._query, tree.root_node)
        except Exception as exc:  # a valid query can still fail at run time
            log.debug("query failed to run: %s", exc)
            return {}

        if isinstance(captures, dict):
            nodes = captures.get(SUBJECT, [])
        else:  # older binding: [(node, capture_name), …]
            nodes = [n for n, name in captures if name == SUBJECT]

        out: dict[tuple[int, int], Subject] = {}
        for node in nodes:
            span = (node.start_byte, node.end_byte)
            out[span] = Subject(
                span=span,
                line=node.start_point[0] + 1,
                text=node.text.decode("utf-8", "replace"),
            )
        return out


# Node types that carry no information for a convention query — punctuation and
# the tree root. Listing them would spend prompt budget teaching nothing.
_VOCAB_NOISE = frozenset({"translation_unit", "comment"})
# Anonymous nodes worth naming: keywords and preprocessor directives, not operators.
_KEYWORDISH = re.compile(r"^#?\w+$")


def node_vocabulary(trees, limit: int = 45) -> str:
    """The node types actually present in this repo, for a prompt.

    Writing a query means naming grammar nodes, and guessing those names is the
    main way a query fails. Two gotchas cost real time even with the grammar in
    front of you: a *named* node is written `(virtual_specifier)` while an
    *anonymous* one must be quoted — `"virtual"` matches, `(virtual)` silently
    compiles to nothing. Splitting the list by that distinction teaches the rule
    without spelling it out, and the names come from the repo rather than from
    whatever the model remembers about tree-sitter-cpp.
    """
    from collections import Counter

    named: Counter = Counter()
    anonymous: Counter = Counter()
    for tree in trees:
        if tree is None:
            continue
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type in _VOCAB_NOISE:
                pass
            elif node.is_named:
                named[node.type] += 1
            elif _KEYWORDISH.match(node.type):
                # Keywords only. Operators (`::`, `&`, `*`) are the noisy half of
                # the anonymous nodes and the structure they imply already has a
                # named node — `pointer_declarator` for `*`. A stray quote or
                # newline in this list is worse than useless in a prompt.
                anonymous[node.type] += 1
            stack.extend(node.children)

    def render(counter: Counter, quote: bool) -> str:
        return " ".join(
            f'"{t}"' if quote else f"({t})" for t, _ in counter.most_common(limit)
        )

    if not named and not anonymous:
        return ""
    return (
        "Node types present in this repository.\n"
        f"Named nodes, written in parentheses:\n{render(named, quote=False)}\n\n"
        f"Anonymous nodes, which MUST be quoted — `\"virtual\"` matches, "
        f"`(virtual)` matches nothing:\n{render(anonymous, quote=True)}"
    )


def syntax_error(source: str) -> str | None:
    """Why this query will not compile, or None if it will.

    The message is kept verbatim because it is unusually good feedback —
    `Invalid node type at row 0, column 41: virtual` names the exact token and
    the exact mistake (an anonymous node written as if it were named). Handing
    that back to the model is far more likely to produce a fix than "your query
    was wrong", which is all a boolean could say.
    """
    if _LANGUAGE is None:
        return "tree-sitter unavailable"
    if not source.strip():
        return "empty query"
    if SUBJECT not in source:
        return f"query has no @{SUBJECT} capture, so there is nothing to count"
    try:
        try:
            Query(_LANGUAGE, source)
        except (TypeError, AttributeError):
            _LANGUAGE.query(source)
    except Exception as exc:
        return str(exc)
    return None


def compile_query(source: str) -> CompiledQuery | None:
    """Compile a query, or None when it is unusable.

    None rather than an exception: an unusable query means "this rule cannot be
    measured", which is an outcome the pipeline already handles. A model-authored
    query being wrong is expected, not exceptional.
    """
    if _LANGUAGE is None or not source.strip():
        return None
    if SUBJECT not in source:
        # Without the capture there is nothing to count. Rejecting here gives a
        # clearer log line than an empty result would.
        log.debug("query has no @%s capture: %s", SUBJECT, source[:80])
        return None
    try:
        try:
            query = Query(_LANGUAGE, source)
        except (TypeError, AttributeError):  # pre-0.25 construction
            query = _LANGUAGE.query(source)
    except Exception as exc:
        log.debug("query did not compile: %s — %s", exc, source[:120])
        return None
    return CompiledQuery(query, source)
