"""Convention learning, inference path — AI-inferred repo-local rules (opt-in).

The statistics path (extractor + learner) can only surface conventions that fit
a template it was given: prefix / suffix / casing. That template is the tool
author's prior, so it finds "conventions the tool knows how to look for", not
necessarily the ones THIS repo's authors care about (docs
convention-detection-design.md — repo-specific rules).

This module does the other thing: it hands the model real code with NO facet
template and asks it to *infer* the conventions THIS repo follows that are not
universal C++. The default path measures; this one guesses. Measured on a
fixture it re-found the `m_` prefix rule unprompted AND inferred `#pragma once`
— a convention the facet system cannot express at all.

Two deliberate boundaries keep it honest, both agreed in the design discussion:

- Inference is open, adoption is not. An inferred rule is a GUESS, not a
  measured rule: it has no verified coverage, so it is written as a
  facet="other" candidate that the deterministic checker skips and a human must
  approve (candidates/ → rules/). Once approved it rides the existing 방안 B
  path — the review LLM judges the diff against it, labelled reproducible=False.
  It never gates CI on its own.
- Cost is opt-in. This sends source code, so tokens scale with repo size, unlike
  the statistics path whose input stays small. Hence a flag, and a char cap.

Two passes, cheap then strong
-----------------------------
Reading a whole repo with the strong model does not scale, and most of what it
would read is irrelevant to the question. So inference is split by difficulty:

1. **Triage (cheap model)** reads a mechanical structure map — directories,
   include edges, classes and their member types (survey.py) — which costs
   nothing to build and fits in one prompt for a whole repo. It answers only
   "where is a project-specific convention likely to live, and which files show
   it", plus any rule the map alone already supports (a one-way include edge is
   a layering rule visible without opening a file).
2. **Inference (strong model)** then reads the full text of *only* those files
   and produces the rules with their machine-runnable checks.

The split follows the same principle as the learner's escalation (design doc
§4.3): a cheap model is good at "is there something here", a strong one is
needed for "what exactly is the rule". If triage finds nothing, the strong pass
does not run at all — which is both the cost saving and the honest answer.

Whatever comes out is still a guess: verifier.py measures each rule against the
repo before anything is written.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from pumpkins.config import (
    LEARN_TEMPERATURE,
    default_learn_model,
    default_reasoning_model,
)
from pumpkins.conventions.extractor import select_files
from pumpkins.conventions.learner import ConventionRule, RuleCheck
from pumpkins.conventions.scope import RuleScope
from pumpkins.conventions.survey import render as render_survey, survey_repo
from pumpkins.llm.provider import get_client

log = logging.getLogger(__name__)

# Source sent to the model is capped so an opt-in run has a bounded cost. Files
# are taken in path order until the budget is spent; the log says how many were
# included so a truncated scan is visible rather than silent.
MAX_INFER_CHARS = 40_000

# How many files the triage pass may nominate. A cap keeps stage 2's cost tied
# to "the interesting parts" rather than to repo size — the whole point of
# triaging. Files past the cap are dropped in the order the model listed them,
# so its own ranking decides what survives.
MAX_LEAD_FILES = 12

_UNSAFE_ID = re.compile(r"[^a-z0-9]+")

_INFER_SYSTEM = """\
You are analyzing a C++ repository to INFER its PROJECT-SPECIFIC conventions:
patterns THIS codebase follows consistently that are NOT universal C++ rules a
generic linter already enforces. Look BEYOND naming — also structural / API
shape / ownership (raw vs smart pointers) / const-correctness / error handling /
file layout — but include naming too when it is clearly consistent.

For each convention you infer:
- rule: ONE sentence, in Korean, phrased so it can directly back a review
  comment (e.g. "헤더는 include 가드 대신 `#pragma once`를 쓴다").
- kind: one of naming | structural | api-shape | ownership | const |
  error-handling | layout | other
- evidence: the concrete thing in the code that made you say it (names, files).
- check: a STRUCTURED, machine-runnable check we use to VERIFY your guess
  against the whole repo. Fill it ONLY when the rule truly reduces to one of:
    * naming — {kind: "naming", category: member|function|class_type|constant,
      facet: prefix|suffix|casing, value: the EXACT string or casing style}.
      Allowed casing values: lowerCamel, lower_snake, UpperCamel, UPPER_SNAKE.
      value for prefix/suffix is the literal affix, e.g. "m_", "_", "Impl".
    * header_directive — {kind: "header_directive", text: the exact first line,
      e.g. "#pragma once"}.
    * return_type (structural) — {kind: "return_type", name_prefix: e.g.
      "create", type_contains: e.g. "unique_ptr"}: functions whose name starts
      with name_prefix return a type whose text contains type_contains. Use for
      factory / ownership return conventions ("make*/create* return unique_ptr").
    * include_direction (structural, LAYERING) — {kind: "include_direction",
      from_dir: e.g. "src/core", forbidden_dir: e.g. "src/ui"}: files under
      from_dir must NOT include headers belonging to forbidden_dir. Use when the
      include edges show a ONE-WAY dependency (A -> B many times, B -> A never).
      Direction matters: from_dir is the layer that must stay independent.
    * base_class (structural, HIERARCHY) — {kind: "base_class", name_suffix:
      e.g. "Exception", base_contains: e.g. "runtime_error"}: classes whose name
      ENDS WITH name_suffix derive from a base whose name contains base_contains.
      Use for "all X derive from Y" conventions.
    * member_ownership (structural, OWNERSHIP) — {kind: "member_ownership",
      value: "smart" | "raw"}: of the class members that hold a pointer,
      value="smart" means they are held by smart pointers (unique_ptr/shared_ptr)
      rather than raw `T*`. Members held by value are not counted either way.
  Otherwise set {kind: "none"}. A wrong check gets the rule REJECTED when its
  measured coverage is low, so only fill it when the rule genuinely reduces to
  that check across ALL of the category (not a subset you cannot express).
  Worked examples — make the check match the rule EXACTLY:
    "멤버 변수는 m_ 접두사를 쓴다" → {kind: naming, category: member,
       facet: prefix, value: "m_"}   (value is the literal affix WITH the
       underscore — "m_", never "m")
    "함수 이름은 lowerCamel" → {kind: naming, category: function,
       facet: casing, value: "lowerCamel"}
    "헤더는 #pragma once로 시작한다" → {kind: header_directive,
       text: "#pragma once"}
    "core 계층은 ui 계층에 의존하지 않는다" → {kind: include_direction,
       from_dir: "src/core", forbidden_dir: "src/ui"}
    "소유하는 멤버 포인터는 스마트 포인터로 잡는다" → {kind: member_ownership,
       value: "smart"}
    "예외 클래스는 std::runtime_error를 상속한다" → {kind: base_class,
       name_suffix: "Exception", base_contains: "runtime_error"}

Be conservative: only patterns you actually see repeated. Do not restate
universal C++ or anything a generic linter owns. Returning few rules is fine.
"""

_TRIAGE_SYSTEM = """\
You are triaging a C++ repository BEFORE a detailed review. You are given only a
mechanical structure map: directories, include edges between them, and classes
with their base classes and member types. No function bodies.

Your job is NOT to state the conventions. It is to say WHERE a project-specific
convention is likely to live and WHICH FILES would show it, so a slower, more
careful pass can read just those.

Look for asymmetries and repetition that only structure reveals:
- one-way include edges between directories (layering)
- consistent ownership shape in member types (raw `T*` vs unique_ptr/shared_ptr)
- families of classes sharing a base, a suffix, or a directory (interfaces,
  handlers, factories)
- a directory whose contents look unlike the rest of the repo

For each lead:
- area: the directory or class family, e.g. "src/core", "*Handler classes"
- suspicion: ONE sentence, in Korean, on the convention you suspect — a
  hypothesis, not a verdict.
- files: 1-3 repo-relative paths from the map that would best show it. Copy the
  paths EXACTLY as they appear in the map.

Return AT MOST 5 leads, most promising first. Fewer is better than padded ones;
returning none is a valid answer when the structure shows nothing specific.
Do not guess about anything the map does not show (naming style inside function
bodies, error handling, comments) — a later pass reads the code for that.
"""


class InferredRule(BaseModel):
    """One convention the model inferred from reading the code (a guess)."""

    rule: str
    kind: str = "other"
    evidence: str = ""
    mechanical_check: str = ""
    checkable_with_ast: bool = False
    check: RuleCheck = Field(default_factory=RuleCheck)


class InferredRuleSet(BaseModel):
    rules: list[InferredRule] = Field(default_factory=list)


class InferenceLead(BaseModel):
    """One place the cheap pass thinks is worth reading in full."""

    area: str = ""
    suspicion: str = ""
    files: list[str] = Field(default_factory=list)


class TriageResult(BaseModel):
    leads: list[InferenceLead] = Field(default_factory=list)


@dataclass
class InferOutcome:
    rules: list[InferredRule]
    input_tokens: int
    output_tokens: int
    files_read: int
    truncated: bool
    # Triage pass. Kept apart from the inference tokens on purpose: the whole
    # claim of the two-stage design is that the cheap pass is cheap, and a claim
    # nobody can check is not a claim.
    leads: list[InferenceLead] = field(default_factory=list)
    triage_input_tokens: int = 0
    triage_output_tokens: int = 0
    triaged: bool = False

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.triage_input_tokens
            + self.triage_output_tokens
        )


def _select_lead_files(files: list[Path], repo: Path, leads: list[InferenceLead]) -> list[Path]:
    """The in-scope files the triage pass nominated, in the order it ranked them.

    Paths are matched leniently (suffix, then basename): the model copies paths
    out of the survey, but a near-miss on a path is a formatting slip, not a
    reason to throw away a good lead. Anything that matches nothing is dropped —
    we never read a file the scan itself excluded, so scoping still holds.
    """
    by_rel = {p.relative_to(repo).as_posix(): p for p in files}
    by_name: dict[str, Path] = {}
    for rel, path in by_rel.items():
        by_name.setdefault(path.name, path)

    picked: list[Path] = []
    for lead in leads:
        for raw in lead.files:
            want = raw.strip().lstrip("./")
            match = by_rel.get(want)
            if match is None:
                match = next(
                    (p for rel, p in by_rel.items() if rel.endswith("/" + want)), None
                )
            if match is None:
                match = by_name.get(want.split("/")[-1])
            if match is None:
                log.debug("triage nominated a path outside the scan: %s", raw)
                continue
            if match not in picked:
                picked.append(match)
    return picked[:MAX_LEAD_FILES]


def _gather_code(
    repo: Path,
    include: list[str] | None,
    exclude: list[str] | None,
    include_tests: bool,
    max_chars: int,
    files: list[Path] | None = None,
) -> tuple[str, int, bool]:
    """Concatenate in-scope C++ sources up to a char budget.

    Reuses the learn scan's file selection so scoping / vendored-dir skipping /
    test exclusion behave exactly as the statistics path does. `files` overrides
    the selection with an explicit list (the triage pass's picks), which is how
    the strong model ends up reading a handful of files instead of the repo."""
    if files is None:
        files = select_files(repo, include, exclude, include_tests)
    parts: list[str] = []
    used = 0
    read = 0
    truncated = False
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.debug("skipping unreadable file %s: %s", path, exc)
            continue
        block = f"// ===== {path.relative_to(repo)} =====\n{text}"
        if used + len(block) > max_chars and parts:
            truncated = True
            break
        parts.append(block)
        used += len(block)
        read += 1
    return "\n\n".join(parts), read, truncated


class RuleInferrer:
    """Infers repo-local conventions by reading code, facet-free.

    Two models by design (module docstring): the cheap learn-tier model triages a
    structure map to decide *where* to look, and the strong review-tier model
    reads only those files to decide *what the rule is*. Naming the two apart
    keeps the cost story explicit — `triage_model` sees a map, `model` sees code.
    """

    def __init__(
        self,
        model: str | None = None,
        triage_model: str | None = None,
        triage: bool = True,
    ):
        self.model = model or default_reasoning_model()
        self.triage_model = triage_model or default_learn_model()
        self.triage_enabled = triage
        self.client = get_client()

    def _triage(
        self,
        repo: Path,
        include: list[str] | None,
        exclude: list[str] | None,
        include_tests: bool,
    ) -> tuple[list[InferenceLead], int, int, str]:
        """Cheap pass: read the structure map, point at what to open."""
        survey = survey_repo(repo, include, exclude, include_tests)
        text = render_survey(survey)
        if not text:
            return [], 0, 0, ""
        parsed = self.client.parse(
            model=self.triage_model,
            max_tokens=1200,
            system=_TRIAGE_SYSTEM,
            user=text,
            temperature=LEARN_TEMPERATURE,
            schema=TriageResult,
        )
        leads = parsed.parsed.leads if parsed.parsed else []
        log.info(
            "inference triage (%s): %d lead(s) from a %d-char structure map; "
            "tokens in=%d out=%d",
            self.triage_model, len(leads), len(text),
            parsed.input_tokens, parsed.output_tokens,
        )
        for lead in leads:
            log.debug("  lead: %s — %s %s", lead.area, lead.suspicion, lead.files)
        return leads, parsed.input_tokens, parsed.output_tokens, text

    def infer(
        self,
        repo: Path,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        include_tests: bool = False,
        max_chars: int = MAX_INFER_CHARS,
    ) -> InferOutcome:
        leads: list[InferenceLead] = []
        triage_in = triage_out = 0
        picked: list[Path] | None = None
        survey_text = ""

        if self.triage_enabled:
            leads, triage_in, triage_out, survey_text = self._triage(
                repo, include, exclude, include_tests
            )
            files = select_files(repo, include, exclude, include_tests)
            picked = _select_lead_files(files, repo, leads)
            if not picked:
                # Either the map showed nothing specific, or every nominated path
                # fell outside the scan. Both mean the same thing for cost: there
                # is no reason to hand the expensive model a repo to read.
                log.info(
                    "inference triage: no files to read closely — skipping the "
                    "%s pass", self.model,
                )
                return InferOutcome(
                    rules=[], input_tokens=0, output_tokens=0, files_read=0,
                    truncated=False, leads=leads, triage_input_tokens=triage_in,
                    triage_output_tokens=triage_out, triaged=True,
                )

        code, files_read, truncated = _gather_code(
            repo, include, exclude, include_tests, max_chars, files=picked
        )
        if not code:
            return InferOutcome(
                rules=[], input_tokens=0, output_tokens=0, files_read=0,
                truncated=False, leads=leads, triage_input_tokens=triage_in,
                triage_output_tokens=triage_out, triaged=self.triage_enabled,
            )
        if truncated:
            log.warning(
                "rule inference: repo exceeds %d char budget — inferred from "
                "first %d file(s) only", max_chars, files_read,
            )

        parsed = self.client.parse(
            model=self.model,
            max_tokens=2000,
            system=_INFER_SYSTEM,
            user=_infer_prompt(code, leads, survey_text),
            temperature=LEARN_TEMPERATURE,
            schema=InferredRuleSet,
        )
        result = parsed.parsed
        rules = result.rules if result else []
        log.info(
            "rule inference (%s): %d rule(s) from %d file(s); tokens in=%d out=%d",
            self.model, len(rules), files_read, parsed.input_tokens, parsed.output_tokens,
        )
        return InferOutcome(
            rules=rules,
            input_tokens=parsed.input_tokens,
            output_tokens=parsed.output_tokens,
            files_read=files_read,
            truncated=truncated,
            leads=leads,
            triage_input_tokens=triage_in,
            triage_output_tokens=triage_out,
            triaged=self.triage_enabled,
        )


def _infer_prompt(code: str, leads: list[InferenceLead], survey_text: str = "") -> str:
    """The strong pass's user message: the structure map, the code, and the hunches.

    The map is included even though the cheap pass already read it, because some
    conventions are only visible in the aggregate. Layering is the clear case:
    "src includes include/yaml-cpp 81 times and never the reverse" is a fact
    about 97 files, and no sample of 7 of them contains it — measured live, the
    strong model proposed zero layering rules until it was given this table.
    It is a page of counts, so it costs a fraction of one source file.

    The suspicions are passed as questions to check, never as conclusions to
    confirm — a cheap model's hunch stated as fact is exactly how a wrong guess
    would acquire unearned authority two steps before the measurement that is
    supposed to catch it.
    """
    parts = []
    if survey_text:
        parts.append(
            "Repository structure (mechanically extracted from ALL in-scope files "
            "— facts, not guesses; the code below is only a sample of it):\n\n"
            f"{survey_text}"
        )
    if leads:
        hints = "\n".join(
            f"- {lead.area}: {lead.suspicion}" for lead in leads if lead.suspicion
        )
        if hints:
            parts.append(
                "A structural pre-scan flagged these areas as possibly holding a "
                "convention. They are UNVERIFIED hunches, not findings — confirm "
                "or ignore them from the evidence below, and report other "
                f"conventions you see regardless of this list.\n\n{hints}"
            )
    parts.append(f"Repository C++ source (a sample — the flagged files):\n\n{code}")
    return "\n\n".join(parts)


def _inferred_id(rule_text: str, taken: set[str]) -> str:
    """Stable, unique, filesystem-safe id from the rule text.

    Deterministic so a re-run inferring the same rule reconciles to the same
    candidate (reconcile dedups by id) instead of piling up duplicates. The
    `ai-` prefix marks it as an AI-inferred guess, not a measured rule."""
    words = _UNSAFE_ID.sub("-", rule_text.lower()).strip("-").split("-")
    base = "ai-" + "-".join(w for w in words if w)[:48].strip("-") or "ai-inferred"
    candidate = base
    n = 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    taken.add(candidate)
    return candidate


def to_convention_rules(
    inferred: list[InferredRule], scan_scope: RuleScope | None = None
) -> list[ConventionRule]:
    """Turn AI-inferred rules into facet="other" candidate rules.

    coverage/occurrences are 0 on purpose: these are guesses, NOT statistically
    measured, so they carry no real numbers and must bypass the threshold gate
    (which is for measured rules) and land in candidates/ for a human. The
    evidence is kept in `examples`; the check hint stays in the console for now.
    """
    scope = scan_scope or RuleScope()
    taken: set[str] = set()
    rules: list[ConventionRule] = []
    for r in inferred:
        rule_text = r.rule.strip()
        if not rule_text:
            continue
        rules.append(
            ConventionRule(
                id=_inferred_id(rule_text, taken),
                category=r.kind or "other",
                description=rule_text,
                facet="other",
                value="",
                coverage=0.0,
                occurrences=0,
                confidence="low",
                examples=[r.evidence] if r.evidence else [],
                scope=scope.model_copy(deep=True),
            )
        )
    return rules
