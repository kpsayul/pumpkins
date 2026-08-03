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
    INPUT_PRICE_PER_MTOK,
    LEARN_TEMPERATURE,
    current_provider,
    default_learn_model,
    default_reasoning_model,
)
from pumpkins.conventions.extractor import select_files
from pumpkins.conventions.learner import ConventionRule, RuleCheck
from pumpkins.conventions.scope import RuleScope
from pumpkins.conventions.survey import render as render_survey, survey_repo
from pumpkins.languages.cpp import ast as cpp_ast, query as cpp_query
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

You are given the files in FULL, so look deliberately at what a structural
summary cannot show and what naming statistics cannot express:
  - modifiers: const, override, static, explicit, noexcept, final
  - where declarations live: namespace placement, declaration order, access blocks
  - how functions begin and end: argument validation, error signalling
These are ordinary, common C++ conventions and they are invisible to every other
part of this tool, so they are the most valuable thing you can find. None of them
fits a shortcut kind — they need `query`.

For each convention you infer:
- rule: ONE sentence, in Korean, phrased so it can directly back a review
  comment (e.g. "헤더는 include 가드 대신 `#pragma once`를 쓴다").
- kind: one of naming | structural | api-shape | ownership | const |
  error-handling | layout | other
- evidence: the concrete thing in the code that made you say it (names, files).
- check: a STRUCTURED, machine-runnable check we use to VERIFY your guess
  against the whole repo. A rule with no check can never be measured, and a rule
  that is never measured is never enforced — so ALWAYS try to fill it.

  `query` is the general kind and your DEFAULT choice; it can express almost any
  convention about code shape. The other kinds below are shortcuts for a handful
  of common cases — use one only when it fits your rule exactly, and reach for
  `query` otherwise. Do not weaken a rule to make it fit a shortcut.

  The kinds:
    * query — THE GENERAL KIND. Prefer it unless a shortcut fits exactly. Two
      tree-sitter queries over the C++ grammar:
        {kind: "query",
         population_query: the sites the rule is ABOUT (the denominator),
         conforming_query: the sites that SATISFY it (the numerator)}
      Both MUST capture the node being judged as @subject, and it must be the
      SAME node in both, or the two cannot be matched up.
      Get the DENOMINATOR right: population is every site the rule governs,
      including the violating ones. "인자 없는 메서드는 const" has a population of
      all no-argument methods, not just the const ones.
      Available node names are listed at the end of the user message;
      anonymous nodes must be quoted.
      Worked examples, both verified to run:
        "인자 없는 메서드는 const 를 붙인다" →
          population: (field_declaration (function_declarator
                        declarator: (field_identifier) @subject (parameter_list)))
          conforming: (field_declaration (function_declarator
                        declarator: (field_identifier) @subject (type_qualifier)))
        "virtual 메서드에는 override 를 쓴다" →
          population: (field_declaration "virtual" (function_declarator
                        declarator: (field_identifier) @subject))
          conforming: (field_declaration (function_declarator
                        declarator: (field_identifier) @subject (virtual_specifier)))
      You may narrow by text with #match?/#eq?, e.g.
        (class_specifier name: (type_identifier) @subject (#match? @subject "Exception$"))
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
  Set {kind: "none"} only when the rule genuinely cannot be checked mechanically
  at all (it needs understanding intent, not shape). Prefer `query` over `none`:
  a rule with no check cannot be measured, so it can never be enforced.
  A wrong check gets the rule REJECTED when its
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
    # Whole-repo mode: how much was actually read, so the coverage claim is
    # checkable rather than asserted.
    chunks_read: int = 0
    chars_read: int = 0
    estimate: "SendEstimate | None" = None

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


# One chunk of source sent in a single call. Sized so a whole chunk plus the
# shared map fits a cheap model comfortably; smaller chunks mean more calls and
# less context per call, larger ones start to lose the middle.
MAX_CHUNK_CHARS = 60_000

# Rough chars-per-token for C++, measured on yaml-cpp (583,391 chars →
# 150,300 tokens). Only ever used to warn about scale before sending, never to
# budget anything, so a real tokenizer would be a dependency for no gain.
CHARS_PER_TOKEN = 3.9


def _stem_key(path: Path) -> tuple[str, str]:
    """Directory + basename without extension — what pairs `foo.h` with `foo.cpp`."""
    return (path.parent.as_posix(), path.stem)


@dataclass(frozen=True)
class FileSlice:
    """A whole file, or a line range of one too big to send in a single call."""

    path: Path
    start_line: int = 1
    end_line: int | None = None   # None → to end of file

    @property
    def is_whole(self) -> bool:
        return self.start_line == 1 and self.end_line is None

    def read(self) -> str:
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.debug("skipping unreadable file %s: %s", self.path, exc)
            return ""
        if self.is_whole:
            return text
        lines = text.splitlines(keepends=True)
        return "".join(lines[self.start_line - 1 : self.end_line])


def _slice_oversized(path: Path, max_chars: int) -> list[FileSlice]:
    """Cut one file into line ranges that each fit a call.

    A single generated or amalgamated header can be several times the budget
    (measured: a vendored 243 KB header in yaml-cpp). Sending only its first
    60 KB would mean claiming to have read the repo while silently skipping 75%
    of that file — the exact kind of quiet gap this tool exists to expose. A seam
    mid-file is bad, but it is visible in the prompt and it keeps the coverage
    claim true.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return [FileSlice(path)]

    out: list[FileSlice] = []
    start = 1
    used = 0
    for i, line in enumerate(lines, start=1):
        if used + len(line) > max_chars and i > start:
            out.append(FileSlice(path, start, i - 1))
            start, used = i, 0
        used += len(line)
    out.append(FileSlice(path, start, len(lines)))
    if len(out) > 1:
        log.info("%s is %d KB — split into %d slices", path.name, sum(map(len, lines)) // 1024, len(out))
    return out


def chunk_files(files: list[Path], max_chars: int = MAX_CHUNK_CHARS) -> list[list[FileSlice]]:
    """Split files into chunks that each fit one call, cutting where code doesn't.

    Two rules, both about not splitting things that only make sense together:

    - a header and its source (`foo.h` + `foo.cpp`) go in the same chunk, so a
      declaration and its definition are never read by two different calls;
    - files stay grouped by directory, because a convention usually lives in a
      module and reading half of one is how a real pattern looks like noise.

    Path order alone would cut straight through both. A single file larger than
    the budget is sliced by line as a last resort, so nothing is silently dropped.
    """
    by_stem: dict[tuple[str, str], list[Path]] = {}
    for path in files:
        by_stem.setdefault(_stem_key(path), []).append(path)

    by_dir: dict[str, list[list[Path]]] = {}
    for (directory, _), group in sorted(by_stem.items()):
        by_dir.setdefault(directory, []).append(sorted(group))

    def size(paths) -> int:
        total = 0
        for p in paths:
            try:
                total += (p.path if isinstance(p, FileSlice) else p).stat().st_size
            except OSError:
                continue
        return total

    chunks: list[list[FileSlice]] = []
    current: list[FileSlice] = []
    used = 0

    def flush() -> None:
        nonlocal current, used
        if current:
            chunks.append(current)
            current, used = [], 0

    for directory in sorted(by_dir):
        for group in by_dir[directory]:
            for path in group:
                grow = size([path])
                if grow > max_chars:
                    # One file bigger than a whole call: flush, then slice it.
                    flush()
                    chunks.extend([s] for s in _slice_oversized(path, max_chars))
                    continue
                if current and used + grow > max_chars:
                    flush()
                current.append(FileSlice(path))
                used += grow
    flush()
    return chunks


@dataclass
class SendEstimate:
    """What an all-of-the-code run is about to send, before it sends it."""

    files: int
    chars: int
    chunks: int
    model: str
    provider: str

    @property
    def tokens(self) -> int:
        return int(self.chars / CHARS_PER_TOKEN)

    @property
    def cost(self) -> float:
        return self.tokens / 1_000_000 * INPUT_PRICE_PER_MTOK.get(self.model, 0.0)

    def describe(self) -> str:
        price = f" · 예상 ${self.cost:.2f}" if self.cost else ""
        return (
            f"{self.files}개 파일 / 약 {self.tokens:,} 토큰을 {self.chunks}번에 나눠 "
            f"{self.provider}({self.model})로 보냅니다{price}"
        )


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

    def _vocabulary(self, picked: list[Path] | None, repo: Path) -> str:
        """Grammar node names present in the code the model is about to read."""
        from pumpkins.conventions.extractor import collect_macros, select_files

        files = picked if picked else select_files(repo)[:40]
        macros = collect_macros(repo, files)
        trees = []
        for path in files:
            try:
                trees.append(cpp_ast.parse_tree(path.read_text(encoding="utf-8", errors="replace"), macros))
            except OSError:
                continue
        return cpp_query.node_vocabulary(trees)

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
            user=_infer_prompt(code, leads, survey_text, self._vocabulary(picked, repo)),
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


    def repair_checks(
        self,
        repo: Path,
        inferred: list[InferredRule],
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        include_tests: bool = False,
    ) -> RepairOutcome:
        """Give the model its broken queries back, once, with the reason.

        Measured before this existed: of 20 checks written while reading a whole
        repo, 17 queries failed to compile and only 3 rules could be measured at
        all. The observations behind them were often correct — the model had read
        the code fine and then could not say how to count it. Losing those to a
        first-draft syntax error is the cheapest possible waste.

        Two design points, both about not making things worse:

        - **Batched.** All broken checks go in one call. Seventeen failures must
          not become seventeen calls, or the retry costs more than the pass it is
          fixing.
        - **Once, and the fix is verified.** A repaired check is accepted only if
          it now diagnoses clean; otherwise the original stays and the rule
          remains unverified. A second attempt is no more trustworthy than the
          first, so it earns its place by working, not by being newer.
        """
        broken: list[tuple[int, InferredRule, str]] = []
        for i, rule in enumerate(inferred):
            reason = diagnose_check(repo, rule.check, include, exclude, include_tests)
            if reason:
                broken.append((i, rule, reason))

        outcome = RepairOutcome(attempted=len(broken))
        if not broken:
            return outcome
        outcome.diagnoses = [f"{r.rule[:40]} — {why}" for _, r, why in broken]

        blocks = []
        for i, rule, why in broken:
            blocks.append(
                f"[{i}] rule: {rule.rule}\n"
                f"    population_query: {rule.check.population_query}\n"
                f"    conforming_query: {rule.check.conforming_query}\n"
                f"    PROBLEM: {why}"
            )
        vocabulary = self._vocabulary(None, repo)
        parsed = self.client.parse(
            model=self.model,
            max_tokens=2000,
            system=_REPAIR_SYSTEM,
            user="\n\n".join(blocks) + (f"\n\n{vocabulary}" if vocabulary else ""),
            temperature=LEARN_TEMPERATURE,
            schema=RepairSet,
        )
        outcome.input_tokens = parsed.input_tokens
        outcome.output_tokens = parsed.output_tokens

        by_index = {i: rule for i, rule, _ in broken}
        for fix in (parsed.parsed.fixed if parsed.parsed else []):
            rule = by_index.get(fix.index)
            if rule is None or not fix.population_query.strip():
                continue
            candidate = rule.check.model_copy(
                update={
                    "kind": "query",
                    "population_query": fix.population_query,
                    "conforming_query": fix.conforming_query,
                }
            )
            if diagnose_check(repo, candidate, include, exclude, include_tests):
                continue  # the retry is broken too — keep the original, stay honest
            inferred[fix.index] = rule.model_copy(update={"check": candidate})
            outcome.repaired += 1

        outcome.still_broken = outcome.attempted - outcome.repaired
        log.info(
            "check repair: %d broken, %d fixed, %d still unmeasurable; tokens in=%d out=%d",
            outcome.attempted, outcome.repaired, outcome.still_broken,
            outcome.input_tokens, outcome.output_tokens,
        )
        return outcome

    def infer_all(
        self,
        repo: Path,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        include_tests: bool = False,
        max_chunk_chars: int = MAX_CHUNK_CHARS,
        model: str | None = None,
    ) -> InferOutcome:
        """Read the ENTIRE repo in chunks and collect every convention observed.

        The point of reading everything is that nothing here decides in advance
        what kind of convention matters. Triage picks files, and picking is a
        judgement about what is interesting; a summary keeps some facts and drops
        others, and that too is a judgement. Both were mine. Reading all of it
        removes the last place my prior could enter.

        What makes this safe is downstream, not here: every observation carries
        its own check, so a hundred of them from a dozen chunks never need to be
        *merged by wording* — each is measured against the whole repo and the
        gate sorts them out. Without that, chunk observations could only be
        combined by asking a model "are these the same?", which would turn the
        one objective signal into an opinion.

        The cheap model reads the code; the expensive one is not involved. That
        is the whole cost argument — measured on yaml-cpp, reading all 97 files
        costs about what reading 6 of them with the strong model costs.
        """
        reader = model or self.model
        files = select_files(repo, include, exclude, include_tests)
        if not files:
            return InferOutcome([], 0, 0, 0, False)

        chunks = chunk_files(files, max_chunk_chars)
        estimate = SendEstimate(
            files=len(files),
            chars=sum(s.path.stat().st_size for c in chunks for s in c if s.is_whole)
            + sum(len(s.read()) for c in chunks for s in c if not s.is_whole),
            chunks=len(chunks),
            model=reader,
            provider=current_provider(),
        )
        # Said before the first call, not after: sending a whole codebase to a
        # third party is the user's decision and they can only make it if the
        # scale is in front of them.
        log.warning("전체 코드 전송: %s", estimate.describe())

        survey_text = render_survey(survey_repo(repo, include, exclude, include_tests))
        vocabulary = self._vocabulary(files[:40], repo)

        rules: list[InferredRule] = []
        in_tok = out_tok = chars = 0
        for i, chunk in enumerate(chunks, start=1):
            code = _render_chunk(chunk, repo)
            if not code.strip():
                continue
            chars += len(code)
            parsed = self.client.parse(
                model=reader,
                max_tokens=2000,
                system=_INFER_SYSTEM,
                user=_chunk_prompt(code, survey_text, vocabulary, i, len(chunks)),
                temperature=LEARN_TEMPERATURE,
                schema=InferredRuleSet,
            )
            in_tok += parsed.input_tokens
            out_tok += parsed.output_tokens
            found = parsed.parsed.rules if parsed.parsed else []
            rules.extend(found)
            log.info(
                "chunk %d/%d (%d file(s), %d chars): %d observation(s)",
                i, len(chunks), len(chunk), len(code), len(found),
            )

        log.info(
            "whole-repo inference: %d observation(s) from %d chunk(s); tokens in=%d out=%d",
            len(rules), len(chunks), in_tok, out_tok,
        )
        return InferOutcome(
            rules=rules,
            input_tokens=in_tok,
            output_tokens=out_tok,
            files_read=len(files),
            truncated=False,
            chunks_read=len(chunks),
            chars_read=chars,
            estimate=estimate,
        )


_REPAIR_SYSTEM = """\
You wrote tree-sitter queries to measure C++ conventions. Some do not work. For
each one you are given the rule, the queries you wrote, and exactly what went
wrong. Rewrite ONLY the queries; the rule text stays as it is.

Two failures and what they mean:
- a compile error names the offending token. "Invalid node type: virtual" almost
  always means an ANONYMOUS node written as if it were named — `"virtual"` is
  correct, `(virtual)` matches nothing. Check the node list at the end.
- "found N sites but 0 satisfy" means the population query is fine and the
  conforming query is wrong. It usually over-constrains: it repeats the whole
  population pattern and adds a requirement in a place the grammar never puts
  it. Write the conforming query as the SIMPLEST pattern that captures the same
  @subject and requires the one distinguishing element.

Both queries must capture the judged node as @subject, and it must be the same
node in both. If you cannot express a rule as queries, return empty strings for
it rather than a guess — an unmeasurable rule is honest, a wrong one is not.
"""


class RepairedCheck(BaseModel):
    index: int
    population_query: str = ""
    conforming_query: str = ""


class RepairSet(BaseModel):
    fixed: list[RepairedCheck] = Field(default_factory=list)


@dataclass
class RepairOutcome:
    """What one repair pass did. Reported, because a silent retry is a lie about
    how well the first attempt worked."""

    attempted: int = 0
    repaired: int = 0
    still_broken: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    diagnoses: list[str] = field(default_factory=list)


# A population this size with zero conforming matches is a broken query, not a
# repo that violates its own convention everywhere. Below it, 0 could be honest.
MIN_POPULATION_TO_SUSPECT_QUERY = 10


def diagnose_check(
    repo: Path,
    check: RuleCheck,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    include_tests: bool = False,
) -> str | None:
    """What is wrong with this query check, in words the model can act on."""
    if check.kind != "query":
        return None
    for label, source in (
        ("population_query", check.population_query),
        ("conforming_query", check.conforming_query),
    ):
        error = cpp_query.syntax_error(source)
        if error:
            return f"{label} does not compile — {error}"

    from pumpkins.conventions.verifier import verify  # lazy: verifier imports us

    result = verify(repo, check, include, exclude, include_tests)
    if result is None or result.total == 0:
        return "population_query matched nothing in the repository"
    if result.matches == 0 and result.total >= MIN_POPULATION_TO_SUSPECT_QUERY:
        return (
            f"population_query found {result.total} sites but conforming_query "
            f"satisfied 0 of them — the conforming query is almost certainly wrong"
        )
    return None


def _render_chunk(chunk: list[FileSlice], repo: Path) -> str:
    parts = []
    for sl in chunk:
        rel = sl.path.relative_to(repo)
        where = "" if sl.is_whole else f" (lines {sl.start_line}-{sl.end_line})"
        parts.append(f"// ===== {rel}{where} =====\n{sl.read()}")
    return "\n\n".join(parts)


def _chunk_prompt(
    code: str, survey_text: str, vocabulary: str, index: int, total: int
) -> str:
    """One slice of the repo, plus the whole-repo map so the slice has bearings.

    A chunk reader cannot tell whether what it sees is a repo-wide convention or
    a local habit — and it does not need to. It proposes; the measurement against
    the whole repo decides. Saying so in the prompt matters: told to report only
    what it is sure of, a reader seeing three instances stays silent, and a
    convention that shows up three times in each of ten chunks is exactly the
    kind the gate exists to confirm.
    """
    parts = [
        f"This is part {index} of {total} of one repository. You are seeing a "
        "slice, so do NOT try to judge whether a pattern holds repo-wide — that "
        "is measured separately afterwards. Report what you observe here, "
        "including patterns you are unsure about; weak observations are filtered "
        "later by measurement, but one you never mention is lost."
    ]
    if survey_text:
        parts.append(
            "Whole-repository map (mechanically extracted from ALL files, for "
            f"orientation — the code below is part {index} of it):\n\n{survey_text}"
        )
    parts.append(f"Source for this part:\n\n{code}")
    if vocabulary:
        parts.append(vocabulary)
    return "\n\n".join(parts)


def _infer_prompt(
    code: str,
    leads: list[InferenceLead],
    survey_text: str = "",
    vocabulary: str = "",
) -> str:
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
    if vocabulary:
        # Query authoring fails mostly on guessed node names, so the real ones go
        # in. Cheap: ~370 tokens for a repo, against ~10k of source.
        parts.append(vocabulary)
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
