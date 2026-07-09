"""Stage 1 — diff collection & parsing.

Runs `git diff` and turns it into a DiffScope: per-file added-line ranges
(new side) plus the raw patch text kept for LLM context.

Parsing is separated from git invocation (`parse_diff_text`) so it can be
unit-tested and later fed PR diffs fetched from GitHub instead of local git.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from unidiff import PatchSet

from cpp_review_bot.config import CPP_EXTENSIONS
from cpp_review_bot.models import DiffScope, FileDiff, LineRange

log = logging.getLogger(__name__)


def collect_diff(repo: Path, base: str | None = None) -> DiffScope:
    """Collect the diff from a local git repo.

    - base given  → `git diff <base>...HEAD` (what a PR against `base` would show)
    - base absent → `git diff HEAD` (uncommitted working-tree changes)
    """
    if base:
        args = ["git", "-C", str(repo), "diff", f"{base}...HEAD"]
    else:
        args = ["git", "-C", str(repo), "diff", "HEAD"]
    log.debug("running: %s", " ".join(args))
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git diff failed: {proc.stderr.strip()}")

    scope = DiffScope(base_ref=base, files=parse_diff_text(proc.stdout))
    log.info(
        "diff: %d changed C++ file(s), %d changed range(s)",
        len(scope.files),
        sum(len(f.added_ranges) for f in scope.files),
    )
    return scope


def parse_diff_text(diff_text: str) -> list[FileDiff]:
    """Parse unified diff text into per-file added-line ranges (C++ files only)."""
    if not diff_text.strip():
        return []

    files: list[FileDiff] = []
    for patched_file in PatchSet(diff_text):
        if patched_file.is_removed_file:
            continue
        path = patched_file.path  # new-side path
        if Path(path).suffix.lower() not in CPP_EXTENSIONS:
            continue

        added_lines: list[int] = []
        for hunk in patched_file:
            for line in hunk:
                if line.is_added and line.target_line_no is not None:
                    added_lines.append(line.target_line_no)

        if not added_lines:
            continue

        files.append(
            FileDiff(
                path=path,
                added_ranges=_merge_into_ranges(added_lines),
                patch_text=str(patched_file),
            )
        )
    return files


def _merge_into_ranges(lines: list[int], gap: int = 3) -> list[LineRange]:
    """Merge sorted line numbers into ranges, joining runs separated by <= gap."""
    ranges: list[LineRange] = []
    for n in sorted(set(lines)):
        if ranges and n - ranges[-1].end <= gap:
            ranges[-1].end = n
        else:
            ranges.append(LineRange(start=n, end=n))
    return ranges
