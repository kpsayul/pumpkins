"""A mechanical map of a repo's structure — the cheap first pass's input.

Inferring structural conventions (layer direction, ownership, module boundaries)
means looking at code, and code is the expensive thing to send a model. But most
of a file is irrelevant to the question: to see that `ui` includes `core` and
`core` never includes `ui`, you do not need a single function body.

So this module extracts the *shape* of the repo — directories, include edges
between them, classes with their base classes and the types of their members —
and renders it as a page or two of text. Two consequences, both deliberate:

- The survey is built with **no LLM at all**. It is parsing, not judgment, so it
  is free, deterministic, and identical on every run.
- It is small enough that the **cheap model** can read the whole repo's shape at
  once, which is what makes a triage pass possible: read the map, point at the
  few places worth opening, and let the strong model read only those.

A survey is intentionally lossy. It answers "where should we look?", never
"what is the rule?" — a lead from here is a hypothesis, and the same
measure-then-gate step applies to whatever it produces (verifier.py).
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from pumpkins.conventions.extractor import select_files
from pumpkins.languages.cpp import ast as cpp_ast, parser as cpp_parser

log = logging.getLogger(__name__)

# Caps on the rendered survey. A repo with thousands of classes would otherwise
# undo the point of surveying — the cheap pass must stay cheap. Truncation is
# reported rather than hidden, the same rule the inference budget follows.
MAX_DIRS = 40
MAX_EDGES = 40
MAX_CLASSES = 60
# Members listed per class: enough to see the ownership shape, not a full header.
MAX_MEMBERS_PER_CLASS = 8
# Below this, a one-way include edge is a coincidence rather than a boundary.
MIN_ONE_WAY_EDGES = 5


@dataclass
class ClassSummary:
    name: str
    path: str
    bases: list[str] = field(default_factory=list)
    member_types: list[str] = field(default_factory=list)
    raw_pointers: int = 0
    smart_pointers: int = 0


@dataclass
class RepoSurvey:
    """What the repo looks like from far away."""

    files_scanned: int = 0
    dir_counts: Counter = field(default_factory=Counter)
    # (from directory, to directory) → number of include lines crossing it
    include_edges: Counter = field(default_factory=Counter)
    classes: list[ClassSummary] = field(default_factory=list)
    raw_pointer_members: int = 0
    smart_pointer_members: int = 0
    engine: str = "regex-fallback"

    @property
    def is_empty(self) -> bool:
        return self.files_scanned == 0

    def one_way_dependencies(self) -> list[tuple[str, str, int]]:
        """(independent dir, dependent dir, edge count) for strictly one-way pairs.

        A layering rule is a claim about the side that stays *independent*, which
        is the opposite end of the arrow you observe. Computing that here keeps
        the model from having to invert it.

        MIN_ONE_WAY_EDGES filters coincidence: two directories with one include
        between them are not a layer boundary, they are two files.
        """
        out = []
        for (src, dst), count in self.include_edges.most_common():
            if count >= MIN_ONE_WAY_EDGES and not self.include_edges.get((dst, src)):
                out.append((dst, src, count))
        return out


def _module_of(rel_path: str, depth: int = 2) -> str:
    """The directory a file belongs to, for layering purposes.

    Two levels deep: one level lumps a whole `src/` into a single node and says
    nothing about direction; deeper than two splits a module into its own
    subfolders and turns internal includes into fake cross-layer edges.
    """
    parts = rel_path.split("/")[:-1]
    return "/".join(parts[:depth]) if parts else "."


def _resolve_include(target: str, by_basename: dict[str, str]) -> str | None:
    """Which module an `#include` points at, or None for a system/external header.

    Matched by file *name* against the repo's own files: include paths are
    written relative to whatever is on the include path, so the text of the
    directive rarely matches the repo-relative path directly. A name that no
    repo file carries is somebody else's header, which is exactly the thing a
    layering rule does not care about.
    """
    return by_basename.get(target.split("/")[-1])


def survey_repo(
    repo: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    include_tests: bool = False,
) -> RepoSurvey:
    """Build the structure map. Uses the learn scan's file selection, so the
    survey covers exactly the code the rules would be measured against."""
    files = select_files(repo, include, exclude, include_tests)
    result = RepoSurvey(files_scanned=len(files), engine=cpp_ast.engine())
    if not files:
        return result

    rel_paths = {p: p.relative_to(repo).as_posix() for p in files}
    by_basename = {p.name: _module_of(rel) for p, rel in rel_paths.items()}
    ast_ok = cpp_ast.require("structure survey")

    texts: dict[Path, str] = {}
    for path in files:
        try:
            texts[path] = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.debug("survey: skipping unreadable %s: %s", path, exc)

    for path, text in texts.items():
        rel = rel_paths[path]
        module = _module_of(rel)
        result.dir_counts[module] += 1

        for line in text.splitlines():
            m = cpp_parser.INCLUDE_RE.match(line)
            if not m:
                continue
            target = _resolve_include(m.group(1), by_basename)
            if target is not None and target != module:
                result.include_edges[(module, target)] += 1

        if not ast_ok:
            continue

        members_by_owner: dict[str, list] = defaultdict(list)
        for member in cpp_ast.members(text):
            members_by_owner[member.owner].append(member)
            result.raw_pointer_members += member.is_raw_pointer
            result.smart_pointer_members += member.is_smart_pointer

        for cls in cpp_ast.classes(text):
            owned = members_by_owner.get(cls.name, [])
            result.classes.append(
                ClassSummary(
                    name=cls.name,
                    path=rel,
                    bases=cls.bases,
                    member_types=[m.type_text for m in owned[:MAX_MEMBERS_PER_CLASS]],
                    raw_pointers=sum(1 for m in owned if m.is_raw_pointer),
                    smart_pointers=sum(1 for m in owned if m.is_smart_pointer),
                )
            )

    return result


def render(survey: RepoSurvey) -> str:
    """The survey as text for a model to read.

    Terse and tabular on purpose: this is read by a cheap model that must spot
    asymmetries (A includes B 40 times, B includes A never), and a table makes
    an asymmetry visible where prose buries it.
    """
    if survey.is_empty:
        return ""

    lines = [f"# Repository structure ({survey.files_scanned} C++ files)", ""]

    lines.append("## Directories (file count)")
    for module, count in survey.dir_counts.most_common(MAX_DIRS):
        lines.append(f"{module}\t{count}")
    if len(survey.dir_counts) > MAX_DIRS:
        lines.append(f"... {len(survey.dir_counts) - MAX_DIRS} more")

    one_way = survey.one_way_dependencies()
    if one_way:
        # Stated in the direction a rule would be written. The raw edge table
        # already contains this, but reading it requires inverting the arrow —
        # "src includes include/ 81 times" has to become "include/ must not
        # include src". Measured live: the model proposed no layering rule at all
        # from the edge table alone, and proposed one immediately from this.
        # The inversion is arithmetic, so it belongs on this side of the wire.
        lines += ["", "## One-way dependencies (candidate layering rules)"]
        for independent, dependent, count in one_way:
            lines.append(
                f"{independent} never includes {dependent}"
                f"\t({dependent} -> {independent}: {count} times)"
            )

    lines += ["", "## Include edges between directories (from -> to, count)"]
    if survey.include_edges:
        for (src, dst), count in survey.include_edges.most_common(MAX_EDGES):
            reverse = survey.include_edges.get((dst, src), 0)
            # The reverse count is spelled out so a one-way dependency reads as
            # one-way without the model having to cross-reference the table.
            lines.append(f"{src} -> {dst}\t{count}\t(reverse: {reverse})")
        if len(survey.include_edges) > MAX_EDGES:
            lines.append(f"... {len(survey.include_edges) - MAX_EDGES} more")
    else:
        lines.append("(none between repo directories)")

    if survey.classes:
        lines += [
            "",
            f"## Classes (raw pointer members: {survey.raw_pointer_members}, "
            f"smart pointer members: {survey.smart_pointer_members})",
        ]
        for cls in survey.classes[:MAX_CLASSES]:
            bases = f" : {', '.join(cls.bases)}" if cls.bases else ""
            types = ", ".join(cls.member_types) if cls.member_types else "-"
            lines.append(f"{cls.name}{bases} @ {cls.path} | {types}")
        if len(survey.classes) > MAX_CLASSES:
            lines.append(f"... {len(survey.classes) - MAX_CLASSES} more classes")
    elif survey.engine != "tree-sitter":
        # Say why the section is missing. An empty class list that looks like
        # "this repo has no classes" would send the triage pass the wrong way.
        lines += ["", "## Classes", "(unavailable — no AST parser in this environment)"]

    return "\n".join(lines)
