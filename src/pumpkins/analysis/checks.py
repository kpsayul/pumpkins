"""clang-tidy check selection for a profile.

The profile definitions themselves live in pumpkins/profiles.py — a profile is
one concept (checks + LLM instructions), and splitting it across modules is how
the LLM half went stale while the check half grew.
"""

from __future__ import annotations

from pumpkins.profiles import PROFILES, get_profile

# Kept as a name so existing imports and `--profile` choices keep working.
CHECK_PROFILES: dict[str, list[str]] = {
    name: profile.clang_tidy_checks for name, profile in PROFILES.items()
}


def checks_arg(profile: str) -> str:
    """Build the value for clang-tidy's --checks= option: disable everything,
    then enable only the profile's checks."""
    return ",".join(["-*", *get_profile(profile).clang_tidy_checks])
