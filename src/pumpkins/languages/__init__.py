"""Language syntax layer, and the per-repo overrides that decide what to read.

`pumpkins/settings.yml` in the target repo, hand-written and committed:

    languages:
      cpp:
        extra_extensions: [".ipp", ".tcc"]   # 이 리포에선 이것도 C++이다
        exclude_extensions: [".inl"]         # 이 리포에선 C++이 아니다

Why per-repo rather than one global list: the same suffix means different things
in different projects. `.h` may be C or C++; `.inc`/`.ipp`/`.tcc` are C++ in some
repos and generated data in others; a repo was seen using `.tc` for YAML test
cases, which a global C++ list would have mis-read as source.

Why `settings.yml` and not the neighbouring `learn-report.yml`: `learn` rewrites that one
on every run, so a hand-edited section there would be destroyed — the exact defect
the rule store was restructured to fix. Reading it needs no rules loaded, so this
also works under `--no-conventions`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pumpkins.config import PUMPKINS_DIRNAME
from pumpkins.languages.cpp import parser as cpp_parser

log = logging.getLogger(__name__)

SETTINGS_FILENAME = "settings.yml"


def settings_path(repo: Path) -> Path:
    return repo / PUMPKINS_DIRNAME / SETTINGS_FILENAME


def _normalize(extensions: object) -> set[str]:
    if not isinstance(extensions, list):
        return set()
    return {f".{str(e).lstrip('.').lower()}" for e in extensions if str(e).strip()}


def _overrides(repo: Path, language: str) -> tuple[set[str], set[str]]:
    """(added, removed) extensions declared by the repo, if it declares any."""
    path = settings_path(repo)
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
        return cpp_parser.EXTENSIONS
    added, removed = _overrides(repo, cpp_parser.NAME)
    if added or removed:
        log.info(
            "%s/%s: C++ extensions %s%s",
            PUMPKINS_DIRNAME,
            SETTINGS_FILENAME,
            f"+{sorted(added)}" if added else "",
            f" -{sorted(removed)}" if removed else "",
        )
    return frozenset((set(cpp_parser.EXTENSIONS) | added) - removed)


def cpp_tu_extensions(repo: Path | None = None) -> frozenset[str]:
    """Extensions compilable on their own — the ones clang-tidy can analyze.

    An added extension only counts as a translation unit if it is not a header
    shape; a repo adding `.ipp` means "more header", not "more .cpp". So the TU
    set is only ever narrowed by the repo, never widened by accident.
    """
    if repo is None:
        return cpp_parser.TU_EXTENSIONS
    _, removed = _overrides(repo, cpp_parser.NAME)
    return frozenset(set(cpp_parser.TU_EXTENSIONS) - removed)
