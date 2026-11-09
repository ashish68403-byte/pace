"""Slice C3b: both arms and the report on the fixture, by hand-computed sets."""

from __future__ import annotations

from provenance.eval.oracle import evaluate, read_oracle, report
from provenance.eval.preregistration import FAILURE_WORDS

#: Oracle id: the oracle's set (c3 moved to m5), lineage's and git log -L's, by commit name.
EXPECTED = {
    "fixture-lib-parse": ({"c1", "c7"}, {"c1", "c7"}, {"c1"}),
    "fixture-views-dispatch": ({"c1", "c2"}, {"c1", "c2"}, {"c1", "c2", "c9"}),
    "fixture-views-helper": ({"c1", "c6", "c10"}, {"c1", "c6", "c10"}, {"c6", "c10"}),
    "fixture-views-inner": ({"c1", "c4"}, {"c1", "c2", "c4"}, {"c1", "c4"}),
    "fixture-views-render": ({"c1", "m5"}, {"c1", "m5"}, {"c1", "m5"}),
    "fixture-views-title": ({"c1"}, {"c1", "c8"}, {"c1"}),
}
#: Oracle id: lineage's identity, the kind, a property part, rounds to follow the chain.
SHAPES = {
    "fixture-lib-parse": ("parse", "top-level", False, 2),
    "fixture-views-dispatch": ("dispatch", "top-level", False, 2),
    "fixture-views-helper": ("helper", "top-level", False, 3),
    "fixture-views-inner": ("outer", "nested", False, 2),
    "fixture-views-render": ("View.render", "method", False, 2),
    "fixture-views-title": ("View.title", "method", True, 2),
}


def _scored(fixture):
    return evaluate(read_oracle(fixture.oracle_dir.glob("*.json")), fixture.clones)


def test_each_functions_three_sets(oracle_repo):
    scored = _scored(oracle_repo)
    sets = {
        item.function.oracle: (
            oracle_repo.names(item.oracle),
            oracle_repo.names(item.lineage),
            oracle_repo.names(item.git_log),
        )
        for item in scored
    }
    assert sets == EXPECTED
    shapes = {
        item.function.oracle: (
            item.resolved.symbol,
            item.resolved.kind,
            item.resolved.property_part,
            item.rounds,
        )
        for item in scored
    }
    assert shapes == SHAPES
    assert {item.files_walked for item in scored} == {3}
    assert [item.projected for item in scored] == [0, 0, 0, 0, 1, 0]


def test_the_report_and_claim_3(oracle_repo):
    found = report(_scored(oracle_repo), {})
    assert found["oracle"] == {"functions": 6, "labels": 12, "projected_labels": 1, "commits": 12}
    assert found["means"]["all"] == {
        "n": 6,
        "lineage": {"precision": 0.8611, "recall": 1.0, "f1": 0.9111},
        "git_log_l": {"precision": 0.9444, "recall": 0.8611, "f1": 0.8778},
    }
    assert found["means"]["rename_or_move"] == {
        "n": 2,
        "lineage": {"precision": 1.0, "recall": 1.0, "f1": 1.0},
        "git_log_l": {"precision": 1.0, "recall": 0.5833, "f1": 0.7333},
    }
    assert found["means"]["nested_or_property"] == {
        "n": 2,
        "lineage": {"precision": 0.5833, "recall": 1.0, "f1": 0.7333},
        "git_log_l": {"precision": 1.0, "recall": 1.0, "f1": 1.0},
    }
    claim = found["claim_3"]
    assert (claim["difference"], claim["n"], claim["threshold"]) == (0.0333, 6, 0.05)
    assert claim["verdict"] == FAILURE_WORDS["identity"]
    rows = {row["oracle"]: row for row in found["functions"]}
    assert rows["fixture-views-inner"]["lineage"]["f1"] == 0.8
    assert rows["fixture-views-inner"]["span"] == [23, 24]
    assert rows["fixture-views-render"]["projected_labels"] == 1
