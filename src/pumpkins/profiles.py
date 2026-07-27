"""Check profiles — one profile, one definition.

A profile answers "what kind of problem are we looking for on this run". Both
halves of that answer live here: the clang-tidy checks to enable, and the
instruction fragment handed to the LLM. They used to sit apart — the check list
in analysis/checks.py, the hunting instructions hardcoded into a single
concurrency-specific system prompt — which made a profile impossible to add
without editing a prompt nobody thought to look at.

That gap had a measured cost: on a real PR the LLM returned an empty verdict
because the prompt only asked about concurrency, while the change's actual
defect was a C++17 construct in a library that supports C++11. The model obeyed
its instructions exactly; the instructions were the wrong ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Profile:
    """One review axis.

    `llm_focus` is spliced into the system prompt as "task 3": the patterns the
    model should hunt for directly in the diff, beyond triaging clang-tidy's
    output. Keep it concrete — vague instructions produce vague findings.
    """

    name: str
    description: str
    llm_focus: str
    clang_tidy_checks: list[str] = field(default_factory=list)
    # Whether this profile needs the project's declared C++ standard in the
    # prompt. Only portability does, and fetching it costs a file read.
    needs_cxx_standard: bool = False


PROFILES: dict[str, Profile] = {
    "concurrency": Profile(
        name="concurrency",
        description="데이터 레이스·락 순서·동기화 오용",
        # Deliberately narrow and high-precision: clang-tidy has weak coverage
        # for lock ordering and unguarded member access, so the LLM carries
        # those and this list stays a precise filter rather than a wide net.
        clang_tidy_checks=[
            "concurrency-*",
            "bugprone-spuriously-wake-up-functions",
            "misc-misplaced-const",
        ],
        llm_focus="""\
Scan the diff for concurrency anti-patterns clang-tidy misses:
- inconsistent lock acquisition order across code paths
- shared members read or written without holding the guarding mutex
- volatile used as a substitute for atomics/synchronization
- condition variable waits without a predicate
- data published between threads without a happens-before edge

Only mutable shared state can race. Data that is immutable after
initialization (const/constexpr tables, string literals) is safe to read from
any number of threads — do not report it.""",
    ),
    "portability": Profile(
        name="portability",
        description="프로젝트가 지원하는 C++ 표준을 넘는 문법·구성",
        # clang-tidy's portability checks are about platform assumptions, not
        # language level; the standard-level judgement is the LLM's job here.
        clang_tidy_checks=["portability-*"],
        needs_cxx_standard=True,
        llm_focus="""\
The project's minimum supported C++ standard is stated above. Scan the diff for
constructs that require a NEWER standard than that minimum — they compile for
you and break the project's own CI.

Common ones, with the standard that introduced them:
- inline variables at namespace scope, `if constexpr`, structured bindings,
  fold expressions, `[[nodiscard]]`, nested namespace definitions (C++17)
- concepts, `consteval`, designated initializers, `<=>`, ranges (C++20)
- generic lambdas, variable templates, relaxed constexpr bodies (C++14)
- `<span>`, `<format>`, `<filesystem>`, `std::optional/variant/string_view`
  headers when the minimum predates them

Report the construct, the standard it needs, and the project's minimum. If the
diff only uses constructs available in the minimum standard, report nothing —
do not speculate about compilers or platforms the project never claimed.""",
    ),
}

DEFAULT_PROFILE = "concurrency"


def get_profile(name: str) -> Profile:
    if name not in PROFILES:
        raise KeyError(f"unknown check profile: {name!r} (known: {sorted(PROFILES)})")
    return PROFILES[name]
