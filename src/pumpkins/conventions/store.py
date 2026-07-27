"""Rule persistence — the `conventions/` directory.

    conventions/
    ├── config.yml       스캔 범위·임계선·통계 (규칙이 왜 이렇게 나왔는지의 근거)
    ├── rules/           활성 — 리뷰가 적용하는 것은 여기뿐
    ├── candidates/      사람 판단 대기 — 리뷰에 영향 없음
    └── archive/         기각 또는 은퇴 (reason이 어느 쪽인지 말해준다)

Two decisions shape this module.

**상태는 디렉터리다.** 규칙이 놓인 폴더가 그 규칙의 상태이므로, 파일 안의
`status:` 필드와 실제가 어긋날 수 없다. 상태 전이는 `git mv` 한 번이고, 그
덕분에 "누가 언제 승인했는가"를 git이 자동으로 기록한다 — 승인자 필드를 손으로
관리할 필요가 없다.

**이력은 git에 맡긴다.** 규칙 하나가 git이 추적하는 파일 하나이므로
`git log --follow conventions/rules/<id>.yml`이 이력이다. 파일 안에 history
배열을 두면 작성자 신원도 서명도 없는 조악한 git 재구현이 된다. 파일에는 git이
줄 수 없는 것 — 결정의 **이유** — 만 남긴다.

규칙당 파일 하나인 이유: learn이 기계적으로 파일을 쓴다. 카테고리별로 묶으면
규칙 하나가 바뀔 때 무관한 규칙까지 재작성돼 git 이력이 더러워지고, 두 사람이
동시에 규칙을 추가하면 충돌한다. 쪼개져 있으면 쓰기가 바뀐 규칙에만 닿는다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml

from pumpkins.config import (
    MIN_RULE_CONSISTENCY,
    MIN_RULE_OCCURRENCES,
    RULE_STATUS_DIRS,
)
from pumpkins.conventions.extractor import CategoryStats
from pumpkins.conventions.learner import ConventionRule
from pumpkins.conventions.scope import RuleScope

log = logging.getLogger(__name__)

Status = Literal["active", "candidate", "archived"]

STATUS_DIRS: dict[Status, str] = dict(RULE_STATUS_DIRS)  # type: ignore[arg-type]

CONFIG_FILENAME = "config.yml"

_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


class StoredRule(ConventionRule):
    """A rule as persisted on disk.

    Extends the measured rule with the bits only a human (or the reconcile step)
    can supply. Deliberately *not* part of the LLM's output schema — the model
    proposes rules, it does not decide their fate.
    """

    reason: str = ""            # 왜 승인/기각/은퇴했는가
    learned_at: str | None = None
    model: str | None = None    # 이 규칙을 판정한 모델


# --------------------------------------------------------------------- paths

def rule_filename(rule_id: str) -> str:
    """Filesystem-safe filename for a rule id (ids come from an LLM)."""
    slug = _UNSAFE_IN_FILENAME.sub("-", rule_id).strip("-.") or "unnamed-rule"
    return f"{slug}.yml"


def status_dir(root: Path, status: Status) -> Path:
    return root / STATUS_DIRS[status]


def is_store_dir(path: Path) -> bool:
    """Whether a path looks like a conventions/ store rather than a legacy file."""
    return path.is_dir()


# ------------------------------------------------------------------- loading

def _load_rule_file(path: Path) -> StoredRule | None:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        return StoredRule.model_validate(doc)
    except Exception as exc:
        log.warning("skipping unreadable rule file %s: %s", path, exc)
        return None


def load_status(root: Path, status: Status) -> dict[str, StoredRule]:
    """Rules in one status, keyed by rule id."""
    directory = status_dir(root, status)
    if not directory.is_dir():
        return {}
    out: dict[str, StoredRule] = {}
    for path in sorted(directory.glob("*.yml")):
        rule = _load_rule_file(path)
        if rule is not None:
            out[rule.id] = rule
    return out


def load_all(root: Path) -> dict[Status, dict[str, StoredRule]]:
    return {status: load_status(root, status) for status in STATUS_DIRS}


def load_active_rules(path: Path) -> list[ConventionRule]:
    """Rules the review stage should enforce.

    Accepts either a `conventions/` store or a legacy single `conventions.yml`,
    so existing repos keep working — new runs write the directory, old files
    still load.
    """
    if is_store_dir(path):
        rules = list(load_status(path, "active").values())
        pending = len(load_status(path, "candidate"))
        if pending:
            log.warning(
                "%d unapproved candidate(s) in %s — move them to %s/ to enforce them",
                pending, status_dir(path, "candidate"), STATUS_DIRS["active"],
            )
        return rules
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"could not parse conventions file {path}: {exc}") from exc
    return [ConventionRule.model_validate(r) for r in (doc or {}).get("rules", [])]


def count_candidates(path: Path) -> int:
    """For the review stage's nudge — 0 for a legacy single file."""
    return len(load_status(path, "candidate")) if is_store_dir(path) else 0


# ----------------------------------------------------------------- reconcile

@dataclass
class Reconciliation:
    """What a re-run of `learn` proposes to change, and what it left alone.

    Nothing here is applied to active rules: a decision already made is the
    user's, and a scan is not a decision. `learn` writes candidates; humans
    move files.
    """

    new_candidates: list[StoredRule] = field(default_factory=list)
    refreshed: list[StoredRule] = field(default_factory=list)
    superseded: list[tuple[str, str]] = field(default_factory=list)  # (active id, candidate id)
    stale: list[str] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def needs_attention(self) -> bool:
        return bool(self.new_candidates or self.superseded or self.stale)


def reconcile(
    existing: dict[Status, dict[str, StoredRule]],
    proposed: list[ConventionRule],
    model: str | None = None,
    reconsider: bool = False,
) -> Reconciliation:
    """Merge a fresh scan into decisions already recorded on disk.

    Pure function over dicts so the five outcomes below are unit-testable
    without touching a filesystem:

    - id 없음                        → 새 후보 (candidates/)
    - 활성 + facet/value 동일        → 근거 수치만 갱신 (결정은 보존)
    - 활성 + 같은 category/facet에 다른 값 → 대체 제안 (활성은 건드리지 않음)
    - archive/에 있음                → 재제안 생략 (기각은 "없음"이 아니라 결정이다)
    - 활성인데 새 스캔이 뒷받침 못함  → 은퇴 후보로 보고 (자동 삭제 금지)
    """
    active, candidates, archived = (
        existing.get("active", {}),
        existing.get("candidate", {}),
        existing.get("archived", {}),
    )
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = Reconciliation()
    seen_active: set[str] = set()
    # (category, facet) → active rule id, for detecting a changed value
    by_facet = {(r.category, r.facet): r.id for r in active.values()}

    for rule in proposed:
        if rule.id in archived and not reconsider:
            result.suppressed.append(rule.id)
            continue

        stored = StoredRule(**rule.model_dump(), learned_at=now, model=model)

        if rule.id in active:
            previous = active[rule.id]
            seen_active.add(rule.id)
            if previous.facet == rule.facet and previous.value == rule.value:
                # Statistics drift as a repo grows; that is not a new decision,
                # so the human's reason survives and the rule stays active.
                stored.reason = previous.reason
                if (previous.coverage, previous.occurrences) == (rule.coverage, rule.occurrences):
                    result.unchanged.append(rule.id)
                else:
                    result.refreshed.append(stored)
                continue

        replaced = by_facet.get((rule.category, rule.facet))
        if replaced is not None and replaced != rule.id:
            seen_active.add(replaced)
            result.superseded.append((replaced, rule.id))
            stored.reason = (
                f"기존 활성 규칙 `{replaced}`을 대체하는 제안입니다 — "
                f"승인하려면 이 파일을 rules/로 옮기고 `{replaced}`을 archive/로 보내세요."
            )
            result.new_candidates.append(stored)
            continue

        if rule.id in candidates:
            stored.reason = candidates[rule.id].reason
        result.new_candidates.append(stored)

    result.stale = sorted(set(active) - seen_active)
    return result


# ------------------------------------------------------------------- writing

_RULE_HEADER = """\
# pumpkins 규칙 — 이 파일이 리뷰 지적의 근거입니다.
#
# 상태는 이 파일이 놓인 디렉터리입니다:
#   rules/       활성 — 리뷰가 적용합니다
#   candidates/  판단 대기 — 리뷰에 영향이 없습니다
#   archive/     기각·은퇴 — learn이 다시 제안하지 않습니다
# 상태를 바꾸려면 파일을 옮기세요:  git mv conventions/candidates/{name} conventions/rules/
#
# 이력은 git이 관리합니다:  git log --follow {path}
# reason에 결정의 이유를 남겨 두면 다음 사람이 되묻지 않습니다.
"""


def write_rule(root: Path, status: Status, rule: StoredRule) -> Path:
    directory = status_dir(root, status)
    directory.mkdir(parents=True, exist_ok=True)
    name = rule_filename(rule.id)
    path = directory / name
    header = _RULE_HEADER.format(
        name=name, path=f"{root.name}/{STATUS_DIRS[status]}/{name}"
    )
    body = yaml.safe_dump(rule.model_dump(), allow_unicode=True, sort_keys=False)
    path.write_text(header + body, encoding="utf-8")
    return path


def write_config(
    root: Path,
    repo: Path,
    model: str,
    stats: list[CategoryStats],
    scan_scope: RuleScope | None = None,
    scanned_files: int | None = None,
    rejected: list[dict] | None = None,
) -> Path:
    """Everything that is *not* a rule: what was measured, and how."""
    root.mkdir(parents=True, exist_ok=True)
    doc = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "repo": str(repo),
        # 규칙을 재현하려면 무엇을 읽었는지가 필요하다.
        "scan": {"files": scanned_files, **(scan_scope or RuleScope()).model_dump()},
        "thresholds": {
            "min_occurrences": MIN_RULE_OCCURRENCES,
            "min_consistency": MIN_RULE_CONSISTENCY,
        },
        "rejected_candidates": rejected or [],
        # "왜 이 규칙이야?"의 원본 근거
        "stats_summary": {
            s.category: {
                "total": s.total,
                "prefixes": s.prefix_counts,
                "suffixes": s.suffix_counts,
                "casing": s.casing_counts,
                "casing_denominator": s.casing_informative,
                "casing_ambiguous": s.casing_ambiguous,
            }
            for s in stats
        },
    }
    header = (
        "# generated by `pumpkins learn` — 규칙이 아니라 규칙의 근거입니다.\n"
        "# 규칙 자체는 rules/ 아래 파일 하나씩 있습니다.\n"
    )
    path = root / CONFIG_FILENAME
    path.write_text(header + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def apply(
    root: Path, result: Reconciliation, accept_all: bool = False
) -> dict[str, list[Path]]:
    """Write the reconciliation out.

    Candidates land in candidates/ unless the caller opted into accept_all —
    `learn` proposes, humans decide (design doc §3-(1)).
    """
    written: dict[str, list[Path]] = {"candidates": [], "refreshed": []}
    target: Status = "active" if accept_all else "candidate"
    for rule in result.new_candidates:
        written["candidates"].append(write_rule(root, target, rule))
    for rule in result.refreshed:
        written["refreshed"].append(write_rule(root, "active", rule))
    return written
