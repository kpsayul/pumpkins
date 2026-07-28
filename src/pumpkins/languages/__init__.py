"""Language syntax layer, and the per-repo overrides that decide what to read.

`.pumpkins.yml` at the repo root, hand-written and committed:

    languages:
      cpp:
        extra_extensions: [".ipp", ".tcc"]   # 이 리포에선 이것도 C++이다
        exclude_extensions: [".inl"]         # 이 리포에선 C++이 아니다

Why per-repo rather than one global list: the same suffix means different things
in different projects. `.h` may be C or C++; `.inc`/`.ipp`/`.tcc` are C++ in some
repos and generated data in others; a repo was seen using `.tc` for YAML test
cases, which a global C++ list would have mis-read as source.

Why a separate file rather than conventions/config.yml: that file is rewritten by
`learn`, so a hand-edited section there would be destroyed on the next run — the
exact defect the rule store was restructured to fix. And the review pipeline needs
this mapping even when run with `--no-conventions`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pumpkins.languages import cpp

log = logging.getLogger(__name__)

CONFIG_FILENAME = ".pumpkins.yml"


def _normalize(extensions: object) -> set[str]:
    if not isinstance(extensions, list):
        return set()
    return {f".{str(e).lstrip('.').lower()}" for e in extensions if str(e).strip()}


def _overrides(repo: Path, language: str) -> tuple[set[str], set[str]]:
    """(added, removed) extensions declared by the repo, if it declares any."""
    path = repo / CONFIG_FILENAME
    if not path.is_file():
        return set(), set()
    try:
        import yaml

        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        section = (doc.get("languages") or {}).get(language) or {}
    except Exception as exc:
        log.warning("ignoring unreadable %s: %s", path, exc)
        return set(), set()
    return (
        _normalize(section.get("extra_extensions")),
        _normalize(section.get("exclude_extensions")),
    )


def cpp_extensions(repo: Path | None = None) -> frozenset[str]:
    """Extensions to treat as C++ in this repo."""
    if repo is None:
        return cpp.EXTENSIONS
    added, removed = _overrides(repo, cpp.NAME)
    if added or removed:
        log.info(
            "%s: C++ extensions %s%s",
            CONFIG_FILENAME,
            f"+{sorted(added)}" if added else "",
            f" -{sorted(removed)}" if removed else "",
        )
    return frozenset((set(cpp.EXTENSIONS) | added) - removed)


def cpp_tu_extensions(repo: Path | None = None) -> frozenset[str]:
    """Extensions compilable on their own — the ones clang-tidy can analyze.

    An added extension only counts as a translation unit if it is not a header
    shape; a repo adding `.ipp` means "more header", not "more .cpp". So the TU
    set is only ever narrowed by the repo, never widened by accident.
    """
    if repo is None:
        return cpp.TU_EXTENSIONS
    _, removed = _overrides(repo, cpp.NAME)
    return frozenset(set(cpp.TU_EXTENSIONS) - removed)
