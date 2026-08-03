"""The closed vocabularies — declared once, as types.

Three sets of words in this project have a fixed, knowable membership: the
categories an identifier can belong to, the casing styles a name can have, and
the two ways a member can hold a pointer. They were declared in four different
files, and two of those had grown their own alias tables to paper over the
drift. That is how a vocabulary breaks: not all at once, but one accommodation
at a time.

Each is declared here **once**, as a `Literal` type, with its runtime set
derived from the same declaration via `get_args`. So the type and the set can
never disagree — there is only one place to change.

Why types and not just constants
--------------------------------
The model fills these fields. A vocabulary written only in the prompt is a
request; a `Literal` in the schema is a constraint the provider enforces.

Measured: `kind` and `facet` were already `Literal` and have never once come
back wrong. `category` and the casing value were free-form `str`, documented in
the prompt — and a model wrote `UpperCamelCase` where `UpperCamel` was required.
Every comparison failed, the rule measured 0/347, and a convention yaml-cpp
follows in every one of its classes was reported as "the repo does not back
this". The prompt had said the right thing. Nothing made it true.

So: if a field has a knowable set of values, it belongs here and it is spelled
as a type. Anything genuinely open — a prefix string like `m_`, a query — stays
free text, because constraining that would be a different mistake.
"""

from __future__ import annotations

from typing import Literal, get_args

# --------------------------------------------------------------- categories

# What the scanner can report for a declaration. Adding one means teaching the
# scanner to produce it; it is not a label anyone may invent.
IdentifierCategory = Literal[
    "private_member", "public_field", "constant", "function", "class_type"
]
CATEGORIES: tuple[str, ...] = get_args(IdentifierCategory)

# What a *rule* may name. A superset, because a rule is allowed to be coarser
# than the scanner: "member" covers both visibilities, and `member_variable` is
# the name members had before they were split by visibility — rules written and
# approved back then must keep working.
RuleCategory = Literal[
    "member", "member_variable",
    "private_member", "public_field", "constant", "function", "class_type",
]
RULE_CATEGORIES: tuple[str, ...] = get_args(RuleCategory)

# Which scanner categories each rule category covers. This replaces the two
# alias tables that had grown separately in the verifier and the checker.
#
# The mapping only ever widens — a coarse rule category covers several observed
# ones. The reverse is deliberately absent: a `private_member` rule must never
# apply to a member whose visibility we could not determine, because guessing is
# how false positives get made.
CATEGORY_SPANS: dict[str, tuple[str, ...]] = {
    "member": ("private_member", "public_field", "member_variable"),
    "member_variable": ("private_member", "public_field", "member_variable"),
    "private_member": ("private_member",),
    "public_field": ("public_field",),
    "constant": ("constant",),
    "function": ("function",),
    "class_type": ("class_type",),
}


def category_covers(rule_category: str, observed: str) -> bool:
    """Whether a rule about `rule_category` judges an identifier seen as `observed`."""
    return observed in CATEGORY_SPANS.get(rule_category, ())


# ------------------------------------------------------------------ casing

# A single-word lowercase name (`flush`) satisfies lowerCamel and lower_snake
# equally, so it carries no casing signal and is counted in its own bucket
# rather than being credited to whichever style a rule happens to ask for.
AMBIGUOUS_CASING = "single_lower"

# What a rule may require. Only styles a name can actually be *held to* —
# `single_lower` and `other` are things the scanner observes, not targets, and a
# rule demanding them would be meaningless.
RuleCasing = Literal["lowerCamel", "lower_snake", "UpperCamel", "UPPER_SNAKE"]
RULE_CASINGS: tuple[str, ...] = get_args(RuleCasing)

# Everything the scanner can report, including the two non-targets.
ObservedCasing = Literal[
    "lowerCamel", "lower_snake", "UpperCamel", "UPPER_SNAKE", "single_lower", "other"
]
OBSERVED_CASINGS: tuple[str, ...] = get_args(ObservedCasing)

# --------------------------------------------------------------- ownership

# How a member holds what it points at. Members held by value are neither — they
# are not part of the ownership question and stay out of its denominator.
OwnershipKind = Literal["smart", "raw"]
OWNERSHIP_KINDS: tuple[str, ...] = get_args(OwnershipKind)
