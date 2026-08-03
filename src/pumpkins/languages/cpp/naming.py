"""Naming-facet vocabulary — how a C++ identifier decomposes into a prefix,
suffix and casing.

This is the "which prefixes / casings exist" knowledge that used to sit inside
[conventions/extractor.py](../../conventions/extractor.py) next to the statistics
machinery. It is a different layer: the extractor *counts* facet distributions
(neutral), this module *defines the facets* (C++/convention knowledge). The
vocabulary is genuinely data — a repo that names members `mFoo` instead of
`m_foo` differs only in these constants — so externalizing it to a settings file
is a natural next step (see the design doc's roadmap).

Nothing here parses code or reads files; it only decomposes a name string.
"""

from __future__ import annotations

import re

# A single lowercase word (`dump`, `value`, `data_`) satisfies lowerCamel and
# lower_snake equally — no word boundary reveals which the project follows.
# Counting it as its own style splits one real convention across buckets and
# hides it from the threshold gate, so it is excluded from casing statistics and
# never counts as a casing violation on the review side.
AMBIGUOUS_CASING = "single_lower"

_CASING_COMPATIBLE = {AMBIGUOUS_CASING: frozenset({"lowerCamel", "lower_snake"})}

# The closed vocabulary `_classify_casing` can produce. A rule asking for a
# casing outside it can never match anything, so comparing against it silently
# yields 0% — and 0% is then reported as "the repo does not follow this rule".
# Measured: a model wrote "UpperCamelCase" instead of "UpperCamel" and a rule
# that yaml-cpp follows for all 347 of its classes was rejected at 0/347.
# An unrecognised value means the check cannot run, not that the rule is false.
KNOWN_CASINGS = frozenset(
    {"lowerCamel", "lower_snake", "UpperCamel", "UPPER_SNAKE", AMBIGUOUS_CASING, "other"}
)


def casing_matches(observed: str, expected: str) -> bool:
    """Whether an identifier's observed casing satisfies a rule's expected one."""
    return observed == expected or expected in _CASING_COMPATIBLE.get(observed, ())


def split_pattern(name: str) -> tuple[str, str, str]:
    """Decompose an identifier into (prefix, suffix, casing) facets.

    e.g. "m_maxCount" -> ("m_", "(none)", "lowerCamel")
         "queue_"     -> ("(none)", "_", "single_lower")
    """
    prefix, core = "(none)", name
    if core.startswith("m_"):
        prefix, core = "m_", core[2:]
    elif core.startswith("s_"):
        prefix, core = "s_", core[2:]
    elif core.startswith("g_"):
        prefix, core = "g_", core[2:]
    elif core.startswith("_"):
        prefix, core = "_", core.lstrip("_")
    # Underscore-less Hungarian prefixes. The uppercase requirement keeps
    # `max`/`mutex`/`kind` out: only `mItemCount`, `kMaxSize` match.
    elif re.match(r"[mksg][A-Z]", core):
        prefix, core = core[0], core[1:]

    suffix = "(none)"
    if core.endswith("_"):
        suffix, core = "_", core.rstrip("_")

    return prefix, suffix, _classify_casing(core)


def _classify_casing(core: str) -> str:
    if not core:
        return "other"
    if re.fullmatch(r"[a-z][a-z0-9]*", core):
        return AMBIGUOUS_CASING  # no word boundary → no casing signal
    if re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+", core):
        return "lower_snake"
    if re.fullmatch(r"[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+", core):
        return "lowerCamel"
    if re.fullmatch(r"[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)*", core):
        return "UpperCamel"
    if re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*", core):
        return "UPPER_SNAKE"
    return "other"
