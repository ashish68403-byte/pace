"""Claims 1 and 2: lineage against blame on the retained dev questions, scored once (slice C4).

docs/tasks/function-identity/004_comparison.md; ADR-0005 §1–§5 and §9; PREREGISTRATION.md
claims 1 and 2. Each question's function gets the arms slices C2 and C3a stored, built as their
censuses counted them and cross-checked against the censuses' committed summaries:

* A, the distinct filtered blame owners of its lines; blame unfiltered, the distinct plain
  owners (labelled, not pre-registered);
* L, its stored `git log -L` commits, and C, the commits of its lineage's versions, both
  without mechanical commits;
* B, the exact chance that a random subset of C as large as A holds the gold.

Recall per arm; paired differences stratified by query class, with D, the MDE and the
difference divided by the gold's validity; claims 1 and 2 in the pre-registered words, the
split of C - A, and the agreement check's qualification beside them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from provenance.analysis.blame_census import _summary as census_summary
from provenance.eval.metrics import (
    CI,
    PairedCI,
    bootstrap_ci,
    chain_hit,
    discordant,
    equal_size_chance,
    mde,
    paired_bootstrap,
)
from provenance.eval.preregistration import COVERAGE_MIN_GAIN, coverage, selection
from provenance.ingest.blame import Target

#: Each question's values in the report: the four arms, then blame unfiltered.
ARMS: Final = ("A", "B", "C", "L", "blame_unfiltered")
#: The arms whose per-question value is 0 or 1: D and the MDE apply between two of them.
BINARY: Final = frozenset({"A", "C", "L", "blame_unfiltered"})
#: (system, baseline): C - A is claim 1, B - A claim 2, C - B the share due to volume,
#: L - A is reported, and blame unfiltered - A is labelled, not pre-registered.
DIFFERENCES: Final = (("C", "A"), ("B", "A"), ("C", "B"), ("L", "A"), ("blame_unfiltered", "A"))


class ComparisonRefused(RuntimeError):
    """The comparison will not be scored; nothing was written."""


@dataclass(frozen=True)
class Arms:
    """One function's commit sets, mechanical commits removed where ADR-0005 §2 says."""

    blame: frozenset[str]
    unfiltered: frozenset[str]
    line_log: frozenset[str]
    lineage: frozenset[str]


def question_values(arms: Arms, gold: str) -> dict[str, float]:
    """One question's value in each arm (ADR-0005 §2–§3): a hit, or B's exact chance."""
    golds = (gold,)
    return {
        "A": chain_hit(arms.blame, golds),
        "B": equal_size_chance(len(arms.blame), arms.lineage, golds),
        "C": chain_hit(arms.lineage, golds),
        "L": chain_hit(arms.line_log, golds),
        "blame_unfiltered": chain_hit(arms.unfiltered, golds),
    }


def summaries(arms: Mapping[tuple[str, str], Arms]) -> dict[str, dict[str, float | int]]:
    """Per-function arm sizes, summarised exactly as the censuses summarise them."""
    ordered = [arms[function] for function in sorted(arms)]
    return {
        "arm_a": census_summary([len(item.blame) for item in ordered]),
        "blame_unfiltered": census_summary([len(item.unfiltered) for item in ordered]),
        "arm_l": census_summary([len(item.line_log) for item in ordered]),
        "arm_c": census_summary([len(item.lineage) for item in ordered]),
    }


def cross_check(
    found: Mapping[str, Any],
    blame_arms: Mapping[str, Any],
    lineage: Mapping[str, Any],
    target: Target,
) -> dict[str, Any]:
    """The arms to be scored are the arms the censuses reported, or nothing is written."""
    expected_inputs = (target.source_sha256, target.pinned, len(target.functions))
    for name, census in (("blame_arms.json", blame_arms), ("lineage.json", lineage)):
        inputs = census["inputs"]
        if (inputs["golden_sha256"], inputs["pinned"], inputs["functions"]) != expected_inputs:
            raise ComparisonRefused(
                f"{name} was made for another golden set, pin or function count"
            )
    expected = {**blame_arms["commits_per_function"], "arm_c": lineage["arm_c_per_function"]}
    if dict(found) != expected:
        raise ComparisonRefused(
            "the arms' sizes differ from the censuses': re-run `pace measure blame` and "
            "`pace measure lineage` on the stored rows first"
        )
    return {"commits_per_function": dict(found), "agrees": True}


def _ci(found: CI) -> dict[str, float | int]:
    return {
        "point": round(found.point, 4),
        "lo": round(found.lo, 4),
        "hi": round(found.hi, 4),
        "n": found.n,
    }


def _recall(rows: Sequence[Mapping[str, Any]], arm: str) -> dict[str, Any]:
    values = [float(row[arm]) for row in rows]
    classes = [str(row["query_class"]) for row in rows]
    by_class = {
        name: _ci(bootstrap_ci([v for v, c in zip(values, classes, strict=True) if c == name]))
        for name in sorted(set(classes))
    }
    return {"all": _ci(bootstrap_ci(values, classes)), "by_class": by_class}


def _difference(
    rows: Sequence[Mapping[str, Any]], system: str, baseline: str, validity: Mapping[str, Any]
) -> tuple[dict[str, Any], PairedCI]:
    ours = [float(row[system]) for row in rows]
    theirs = [float(row[baseline]) for row in rows]
    paired = paired_bootstrap(ours, theirs, [str(row["query_class"]) for row in rows])
    found: dict[str, Any] = {
        "difference": round(paired.delta, 4),
        "interval": [round(paired.lo, 4), round(paired.hi, 4)],
        "n": paired.n,
        "prob_not_better": round(paired.prob_not_better, 4),
    }
    if system in BINARY and baseline in BINARY:
        d, n = discordant(ours, theirs)
        found["discordant"] = d
        found["mde"] = None if d == 0 else round(mde(n, d / n), 4)
    rate = float(validity["rate"])
    low, high = (float(x) for x in validity["ci95"])
    # ADR-0004 §8: the raw difference is the claim; divided, it shows what empty gold may hide.
    found["over_validity"] = {
        "at_point": round(paired.delta / rate, 4),
        "at_upper": round(paired.delta / high, 4),
        "at_lower": round(paired.delta / low, 4),
    }
    return found, paired


def report(
    rows: Sequence[Mapping[str, Any]],
    *,
    inputs: Mapping[str, Any],
    cross_checked: Mapping[str, Any],
    validity: Mapping[str, Any],
    qualification: Mapping[str, Any],
) -> dict[str, Any]:
    """The table, the decisions and the qualification, from the per-question rows."""
    differences: dict[str, Any] = {}
    paired: dict[str, PairedCI] = {}
    for system, baseline in DIFFERENCES:
        name = f"{system}-{baseline}"
        differences[name], paired[name] = _difference(rows, system, baseline, validity)
    c_a, b_a, c_b = paired["C-A"], paired["B-A"], paired["C-B"]
    return {
        "schema": 1,
        "inputs": dict(inputs),
        "cross_check": dict(cross_checked),
        "validity": dict(validity),
        "recall": {arm: _recall(rows, arm) for arm in ARMS},
        "arm_sizes": {
            "A": census_summary([int(row["size_a"]) for row in rows]),
            "C": census_summary([int(row["size_c"]) for row in rows]),
            "L": census_summary([int(row["size_l"]) for row in rows]),
        },
        "differences": differences,
        "claim_1": {
            "difference": round(c_a.delta, 4),
            "interval": [round(c_a.lo, 4), round(c_a.hi, 4)],
            "threshold": COVERAGE_MIN_GAIN,
            "verdict": coverage(c_a.delta, c_a.lo),
        },
        "claim_2": {
            "difference": round(b_a.delta, 4),
            "interval": [round(b_a.lo, 4), round(b_a.hi, 4)],
            "reading": selection(b_a.lo, b_a.hi),
        },
        "split": {
            "C-A": round(c_a.delta, 4),
            "C-B": round(c_b.delta, 4),
            "B-A": round(b_a.delta, 4),
            "adds_up": abs(c_a.delta - (c_b.delta + b_a.delta)) < 1e-9,
        },
        "qualification": dict(qualification),
    }


def render_json(found: Mapping[str, Any]) -> str:
    """Sorted keys and no timestamps: the same rows always render the same bytes."""
    return json.dumps(found, indent=2, sort_keys=True) + "\n"


def per_question_jsonl(rows: Sequence[Mapping[str, Any]]) -> str:
    """One line per question, in qid order: every number in the report comes from these."""
    return "".join(json.dumps(dict(row), sort_keys=True) + "\n" for row in rows)
