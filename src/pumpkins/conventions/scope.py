"""Path scoping — which files a rule (or a learn scan) applies to.

One repository rarely has one convention. Legacy subtrees, generated code and
vendored dependencies follow different rules, and headers often differ from
translation units. So both ends of the convention axis are scoped by the same
vocabulary: `pumpkins learn` narrows what it *reads*, and each rule in
conventions.yml narrows what it *judges*
(docs/convention-detection-design.md §3-(2)).

Patterns are matched case-sensitively against the repo-relative POSIX path, so
the same conventions.yml produces the same findings on every platform. `*`
crosses directory separators, and a pattern naming a directory covers
everything beneath it:

    "src/legacy"        → src/legacy/a/b.cpp        ✓
    "src/factory/**"    → src/factory/impl/f.cpp    ✓
    "*_generated.hpp"   → model/x_generated.hpp     ✓
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from pydantic import BaseModel, Field, field_validator


def normalize_path(path: str) -> str:
    """Repo-relative path in the form patterns are matched against."""
    return str(PurePosixPath(path.replace("\\", "/")))


def path_matches(path: str, patterns: list[str]) -> bool:
    """Whether a repo-relative path matches any of the patterns."""
    for pattern in patterns:
        pattern = pattern.replace("\\", "/").rstrip("/")
        if fnmatchcase(path, pattern) or fnmatchcase(path, f"{pattern}/*"):
            return True
    return False


class RuleScope(BaseModel):
    """Where a rule applies. Every field empty (the default) means everywhere.

    `exclude_paths` wins over `paths`, so the common shape — "the whole repo
    except this legacy corner" — needs only the exclusion.
    """

    paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)

    @field_validator("extensions")
    @classmethod
    def _normalize_extensions(cls, value: list[str]) -> list[str]:
        return [f".{e.lstrip('.').lower()}" for e in value]

    @property
    def is_repo_wide(self) -> bool:
        return not (self.paths or self.exclude_paths or self.extensions)

    def applies_to(self, path: str) -> bool:
        """Whether a rule with this scope should judge the given file."""
        if self.is_repo_wide:
            return True
        path = normalize_path(path)
        if self.extensions and PurePosixPath(path).suffix.lower() not in self.extensions:
            return False
        if self.exclude_paths and path_matches(path, self.exclude_paths):
            return False
        if self.paths and not path_matches(path, self.paths):
            return False
        return True

    def describe(self) -> str:
        """One-line human summary, used in review comments as the rule's reach."""
        parts = []
        if self.paths:
            parts.append(f"경로 {', '.join(self.paths)}")
        if self.extensions:
            parts.append(f"확장자 {', '.join(self.extensions)}")
        if self.exclude_paths:
            parts.append(f"제외 {', '.join(self.exclude_paths)}")
        return " / ".join(parts) if parts else "리포 전체"
