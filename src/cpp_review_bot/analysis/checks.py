"""Check profiles for clang-tidy.

Extensible by design: add a new bug class as a new key. The initial focus is
concurrency anti-patterns. Note that clang-tidy alone has weak coverage for
lock-ordering inversions and unguarded shared-member access — those are also
hunted by the LLM stage directly from diff context (see llm/postprocess.py),
so this list intentionally stays high-precision rather than exhaustive.
"""

from __future__ import annotations

CHECK_PROFILES: dict[str, list[str]] = {
    # Concurrency anti-patterns: MT-unsafe libc calls, condvar misuse, etc.
    "concurrency": [
        "concurrency-*",
        "bugprone-spuriously-wake-up-functions",
        "misc-misplaced-const",  # const on pointee vs pointer confusion around shared state
    ],
    # Future profiles (examples):
    # "memory": ["bugprone-use-after-move", "clang-analyzer-cplusplus.*", ...],
    # "ub":     ["clang-analyzer-core.*", "bugprone-signed-char-misuse", ...],
}


def checks_arg(profile: str) -> str:
    """Build the value for clang-tidy's --checks= option: disable everything,
    then enable only the profile's checks."""
    if profile not in CHECK_PROFILES:
        raise KeyError(f"unknown check profile: {profile!r} (known: {sorted(CHECK_PROFILES)})")
    return ",".join(["-*", *CHECK_PROFILES[profile]])
