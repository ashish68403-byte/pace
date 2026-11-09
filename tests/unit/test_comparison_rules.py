"""Slice C4: a question's values, the census cross-check and the decisions, on hand-made arms."""

from __future__ import annotations

import pytest

from provenance.eval.comparison import (
    Arms,
    ComparisonRefused,
    cross_check,
    question_values,
    report,
    summaries,
)
from provenance.eval.preregistration import coverage, selection
from provenance.ingest.blame import Target

ARMS = Arms(
    blame=frozenset({"a1"}),
    unfiltered=frozenset({"a1", "m1"}),
    line_log=frozenset({"a1", "c2", "l1"}),
    lineage=frozenset({"a1", "c2", "c3", "c4"}),
)


def test_a_questions_values():
    assert question_values(ARMS, "a1") == {
        "A": 1.0,
        "B": 0.25,
        "C": 1.0,
        "L": 1.0,
        "blame_unfiltered": 1.0,
    }
    assert question_values(ARMS, "c3") == {
        "A": 0.0,
        "B": 0.25,
        "C": 1.0,
        "L": 0.0,
        "blame_unfiltered": 0.0,
    }
    assert set(question_values(ARMS, "zz").values()) == {0.0}


TWO = {
    ("p.py", "f"): ARMS,
    ("p.py", "g"): Arms(
        frozenset({"x"}), frozenset({"x"}), frozenset({"x"}), frozenset({"x", "y"})
    ),
}
TARGET = Target(pinned="pin", questions=2, functions=tuple(sorted(TWO)), source_sha256="sha")


def _censuses(found):
    inputs = {"golden_sha256": "sha", "pinned": "pin", "functions": 2}
    blame = {
        "inputs": dict(inputs),
        "commits_per_function": {key: found[key] for key in ("arm_a", "blame_unfiltered", "arm_l")},
    }
    return blame, {"inputs": dict(inputs), "arm_c_per_function": found["arm_c"]}


def test_the_cross_check_passes_the_reported_arms_and_refuses_others():
    found = summaries(TWO)
    blame, lineage = _censuses(found)
    assert cross_check(found, blame, lineage, TARGET)["agrees"] is True
    blame["commits_per_function"]["arm_a"] = {**found["arm_a"], "max": 99}
    with pytest.raises(ComparisonRefused, match="differ from the censuses"):
        cross_check(found, blame, lineage, TARGET)
    blame, lineage = _censuses(found)
    lineage["inputs"]["pinned"] = "another"
    with pytest.raises(ComparisonRefused, match="another golden set"):
        cross_check(found, blame, lineage, TARGET)


VALIDITY = {"status": "current", "source": "audit.json", "rate": 0.8, "ci95": [0.5, 1.0]}
QUALIFICATION = {"source": "lineage.json", "agreement_check": {"verdict": "misses"}}


def _rows():
    rows = []
    cases = (
        ("provenance", "a1"),
        ("provenance", "c3"),
        ("archaeology", "zz"),
        ("archaeology", "c2"),
    )
    for index, (query_class, gold) in enumerate(cases):
        rows.append(
            {
                "qid": f"q{index}",
                "query_class": query_class,
                "path": "p.py",
                "function": "f",
                "gold": gold,
                "size_a": 1,
                "size_c": 4,
                "size_l": 3,
                **question_values(ARMS, gold),
            }
        )
    return rows


def test_the_decisions_are_the_pre_registered_words_and_the_split_adds_up():
    found = report(
        _rows(), inputs={}, cross_checked={}, validity=VALIDITY, qualification=QUALIFICATION
    )
    claim_1, claim_2 = found["claim_1"], found["claim_2"]
    assert claim_1["verdict"] == coverage(claim_1["difference"], claim_1["interval"][0])
    assert claim_2["reading"] == selection(*claim_2["interval"])
    assert found["split"]["adds_up"] is True
    assert found["split"]["C-A"] == pytest.approx(
        found["split"]["C-B"] + found["split"]["B-A"], abs=1e-4
    )
    differences = found["differences"]
    assert "mde" in differences["C-A"] and "mde" not in differences["B-A"]
    assert differences["C-A"]["over_validity"]["at_point"] == pytest.approx(0.5 / 0.8, abs=1e-4)
    assert found["recall"]["C"]["all"]["point"] == 0.75
    assert found["qualification"] == QUALIFICATION
