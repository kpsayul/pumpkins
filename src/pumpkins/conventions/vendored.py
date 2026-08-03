"""Which parts of a repo are somebody else's code.

Learning conventions from vendored code teaches you the conventions of a project
nobody here works on. It has cost this tool twice, both measured:

- fmt: a bundled `test/gtest/` supplied 2,837 of 2,845 UpperCamel names, and a
  uniformly snake_case codebase measured as 63% UpperCamel.
- yaml-cpp: `src/contrib/` is 6 of 97 files but **44% of the characters**, and
  157 of the repo's 158 "constants" are in it. A run produced the rule "상수는
  lower_snake" at 100% coverage over 99 occurrences — every one of them from
  someone else's file, offered to a human for approval as this repo's practice.

The old defence was a list of directory names I typed (`third_party`, `vendor`,
…). It missed `test/gtest` and it misses `src/contrib`, and a longer list is
just a later miss. So this module asks the repo instead, in descending order of
how much the answer is a fact rather than a guess:

1. **Submodules.** `.gitmodules` says the path is another project. Not evidence
   of foreignness — a declaration of it.
2. **A different copyright holder.** A file carrying an SPDX or copyright line
   naming someone other than this repo's usual holder is stating its own
   provenance. Works whether the repo headers everything (vendored files name a
   different owner) or nothing (a lone header is the outlier).
3. **Code nobody maintains.** Bytes per commit. Measured on yaml-cpp:
   `dragonbox.h` is ~40 KB/commit against a repo median near 300 B/commit — a
   150× gap, not a borderline call. Imported code is written once and never
   touched; the team's own code is edited.

Every exclusion is reported with the reason that produced it, because this is a
judgement about someone's repo and they have to be able to overrule it. Silent
exclusion would be the same failure as silent inclusion, just quieter.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# A file must be at least this big before low churn means anything. A small file
# with two commits is ordinary; a 200 KB one with two commits was pasted in.
MIN_CHURN_CANDIDATE_BYTES = 50_000
# How far above the repo's median bytes-per-commit counts as "nobody edits this".
# Measured gap on yaml-cpp was ~150×, so this is not a knife-edge.
CHURN_OUTLIER_FACTOR = 20
# Below this the history is too short to say anything about who maintains what.
MIN_REPO_COMMITS = 50
# How much more common the repo's usual copyright holder must be before a
# different one counts as foreign. Measured: yaml-cpp is 96 files with no header
# against 1 naming another author — not a close call. The factor exists so a
# repo that headers only part of its own code is not accused of vendoring it.
FOREIGN_HOLDER_FACTOR = 3

# `Copyright (c) 2020 Someone`, `SPDX-FileCopyrightText: 2020-2024 Someone`
_COPYRIGHT_RE = re.compile(
    r"(?:SPDX-FileCopyrightText:|Copyright)\s*(?:\(c\)|©)?\s*[\d,\s-]*\s*(.+?)\s*$",
    re.IGNORECASE,
)
# Only the top of a file declares provenance; further down it is quoted text.
_HEADER_LINES = 30


@dataclass
class VendorFinding:
    """One path judged to be someone else's code, and why."""

    path: str
    reasons: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return f"{self.path} ({', '.join(self.reasons)})"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    return proc.stdout if proc.returncode == 0 else ""


def submodule_paths(repo: Path) -> set[str]:
    """Paths `.gitmodules` declares as other projects."""
    text = _git(repo, "config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$")
    return {line.split(maxsplit=1)[1].strip() for line in text.splitlines() if " " in line}


def copyright_holder(text: str) -> str | None:
    """The copyright holder a file declares at its top, if any."""
    for line in text.splitlines()[:_HEADER_LINES]:
        stripped = line.strip().lstrip("/*# ").strip()
        if not stripped:
            continue
        match = _COPYRIGHT_RE.search(stripped)
        if match:
            holder = match.group(1).strip(" .*/")
            # `Copyright (c) 2024 ACME. All rights reserved.` — the boilerplate
            # tail is not part of the name and would make every holder unique.
            holder = re.split(r"[.,;]?\s*all rights reserved", holder, flags=re.I)[0]
            holder = holder.strip(" .*/")
            if holder and not holder.lower().startswith("all rights"):
                return holder
    return None


def _commits_per_file(repo: Path) -> dict[str, int]:
    """How many commits touched each path, from ONE git call.

    A `git log` per file is fine for a hundred files and unusable for five
    thousand, and this runs on every scan."""
    counts: dict[str, int] = {}
    for line in _git(repo, "log", "--format=%H", "--name-only").splitlines():
        line = line.strip()
        if line and "/" in line or (line and "." in line):
            counts[line] = counts.get(line, 0) + 1
    return counts


def _churn_outliers(repo: Path, files: list[Path]) -> dict[Path, str]:
    """Files far too large for how rarely anyone has edited them."""
    total_commits = len(_git(repo, "log", "--format=%H").splitlines())
    if total_commits < MIN_REPO_COMMITS:
        return {}

    commits = _commits_per_file(repo)
    ratios: dict[Path, float] = {}
    sizes: dict[Path, int] = {}
    for path in files:
        try:
            sizes[path] = path.stat().st_size
        except OSError:
            continue
        touched = commits.get(path.relative_to(repo).as_posix(), 0)
        if touched:
            ratios[path] = sizes[path] / touched

    if len(ratios) < 5:
        return {}
    ordered = sorted(ratios.values())
    median = ordered[len(ordered) // 2]
    if median <= 0:
        return {}

    return {
        path: (
            f"거의 수정되지 않음 (커밋당 {ratio / 1024:.0f}KB, "
            f"리포 중앙값의 {ratio / median:.0f}배)"
        )
        for path, ratio in ratios.items()
        if sizes.get(path, 0) >= MIN_CHURN_CANDIDATE_BYTES
        and ratio >= median * CHURN_OUTLIER_FACTOR
    }


_CACHE: dict[tuple, list["VendorFinding"]] = {}


def detect(repo: Path, files: list[Path]) -> list[VendorFinding]:
    """Files that look like someone else's code, each with its reason.

    Reasons accumulate: a file flagged by two signals says so, which is exactly
    the case a human should trust without checking.
    """
    key = (repo, len(files), files[0] if files else None, files[-1] if files else None)
    if key in _CACHE:
        return _CACHE[key]

    findings: dict[Path, VendorFinding] = {}

    def flag(path: Path, reason: str) -> None:
        rel = path.relative_to(repo).as_posix()
        findings.setdefault(path, VendorFinding(rel)).reasons.append(reason)

    submodules = submodule_paths(repo)
    holders: dict[Path, str | None] = {}
    for path in files:
        rel = path.relative_to(repo).as_posix()
        if any(rel == s or rel.startswith(s.rstrip("/") + "/") for s in submodules):
            flag(path, "git 서브모듈")
        try:
            holders[path] = copyright_holder(
                path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            holders[path] = None

    # The repo's usual holder — most often None, when a project headers nothing.
    # A holder counts as foreign only if it is a *strict minority*: with two
    # files declaring two different owners there is no majority to be foreign to,
    # and guessing one would flag whichever happened to sort first.
    counts: dict[str | None, int] = {}
    for holder in holders.values():
        counts[holder] = counts.get(holder, 0) + 1
    dominant = max(counts, key=counts.get) if counts else None
    for path, holder in holders.items():
        if holder is None or holder == dominant:
            continue
        # Foreign means *rare*, not merely different. A repo that headers only
        # some of its own files would otherwise report the headered ones as
        # somebody else's — 40 files saying ACME against 60 saying nothing is a
        # house style applied unevenly, not an import.
        if counts[dominant] >= counts[holder] * FOREIGN_HOLDER_FACTOR:
            flag(path, f"저작권자가 다름: {holder[:40]}")

    for path, reason in _churn_outliers(repo, files).items():
        flag(path, reason)

    result = sorted(findings.values(), key=lambda f: f.path)
    _CACHE[key] = result
    return result
