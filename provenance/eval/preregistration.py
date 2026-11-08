"""The pre-registration in code: its thresholds, and the rules that decide each claim (slice C1).

`docs/PREREGISTRATION.md` was committed before any blame, lineage or comparison number existed
(ADR-0005). This module repeats its numbers and its words, so the comparison (slice C4) states
mechanically whether each claim held, in the words fixed in advance: a rule returns HOLDS, or
the sentence the pre-registration wrote for that claim's failure. A unit test reads the
committed file and fails if the two ever disagree.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

COVERAGE_MIN_GAIN: Final = 0.05
IDENTITY_MIN_F1_GAIN: Final = 0.05
AGREEMENT_MIN_F1: Final = 0.90
LEXICAL_CEILING: Final = 0.35
REFUSAL_MIN_GAIN: Final = 0.05

THRESHOLDS: Final[dict[str, float]] = {
    "COVERAGE_MIN_GAIN": COVERAGE_MIN_GAIN,
    "IDENTITY_MIN_F1_GAIN": IDENTITY_MIN_F1_GAIN,
    "AGREEMENT_MIN_F1": AGREEMENT_MIN_F1,
    "LEXICAL_CEILING": LEXICAL_CEILING,
    "REFUSAL_MIN_GAIN": REFUSAL_MIN_GAIN,
}

HOLDS: Final = "holds"

#: Claim 2's three readings of B - A's interval, fixed before the result.
SELECTION_ABOVE: Final = "lineage picks better commits even on a set tilted against it"
SELECTION_CONTAINS: Final = "lineage's gain is volume"
SELECTION_BELOW: Final = "blame is more precise per commit, and lineage's gain is coverage alone"

#: What the write-up says when a claim fails: these words, and no others.
FAILURE_WORDS: Final[dict[str, str]] = {
    "coverage": (
        "Walking the function's lineage did not recover measurably more rationale than git "
        "blame on this benchmark."
    ),
    "identity": (
        "The lineage layer did not track Python functions measurably better than git log -L on "
        "CodeShovel's oracle."
    ),
    "lexical": (
        "On the test split, a lexical baseline reaches the gold often enough that the benchmark "
        "does not separate retrieval from string matching."
    ),
    "refusal": "Refusal did not buy measurable accuracy over answering everything.",
}

_ROW = re.compile(r"^\| `(?P<name>[A-Z0-9_]+)` \| (?P<value>[0-9.]+) \|", re.M)


def read_thresholds(path: Path) -> dict[str, float]:
    """The Thresholds table of a pre-registration file, as {constant: value}."""
    text = path.read_text(encoding="utf-8")
    return {match["name"]: float(match["value"]) for match in _ROW.finditer(text)}


def coverage(delta: float, lo: float) -> str:
    """Claim 1: C - A >= COVERAGE_MIN_GAIN, with its interval's lower end above 0."""
    return HOLDS if delta >= COVERAGE_MIN_GAIN and lo > 0 else FAILURE_WORDS["coverage"]


def selection(lo: float, hi: float) -> str:
    """Claim 2: the pre-registered reading of B - A's interval."""
    if lo > 0:
        return SELECTION_ABOVE
    if hi < 0:
        return SELECTION_BELOW
    return SELECTION_CONTAINS


def identity(gain: float, lo: float) -> str:
    """Claim 3: lineage's F1 beats git log -L's by IDENTITY_MIN_F1_GAIN, its lower end above 0."""
    return HOLDS if gain >= IDENTITY_MIN_F1_GAIN and lo > 0 else FAILURE_WORDS["identity"]


def lexical(upper: float) -> str:
    """Claim 4: the strongest lexical baseline's Wilson upper bound stays below the ceiling."""
    return HOLDS if upper < LEXICAL_CEILING else FAILURE_WORDS["lexical"]
