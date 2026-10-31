"""ADR-0004 §7's linkage-bias characterisation: the pure half, no database."""

from __future__ import annotations

import json

import pytest

from provenance.analysis.linkage_bias import (
    OTHER,
    TOP_LEVEL,
    CommitFacts,
    module_distribution,
    module_of,
    summarise,
    total_variation,
)
from provenance.ingest.message_pool import LINKED, NO_REFERENCE


def test_a_module_is_the_first_directory_under_the_root():
    assert module_of("airflow-core/src/airflow/jobs/scheduler_job_runner.py") == "jobs"
    assert module_of("airflow-core/src/airflow/models/dag/x.py") == "models"
    assert module_of("airflow-core/src/airflow/settings.py") == TOP_LEVEL
    assert module_of("providers/x.py") == OTHER
    assert module_of("core/src/pkg/alpha.py", "core/src/") == "pkg"


def test_a_commit_spreads_one_unit_of_weight_over_its_modules():
    touched = [frozenset({"jobs"}), frozenset({"jobs", "models"}), frozenset()]
    assert module_distribution(touched) == {"jobs": 0.75, "models": 0.25}


def test_total_variation_is_zero_for_identical_and_one_for_disjoint():
    assert total_variation({"a": 0.5, "b": 0.5}, {"a": 0.5, "b": 0.5}) == 0.0
    assert total_variation({"a": 1.0}, {"b": 1.0}) == 1.0
    assert total_variation({"a": 0.75, "b": 0.25}, {"a": 0.25, "b": 0.75}) == pytest.approx(0.5)


def test_the_summary_compares_groups_and_names_no_author():
    groups = {
        LINKED: [
            CommitFacts(frozenset({"jobs"}), "Ada Lovelace", 2020),
            CommitFacts(frozenset({"models"}), "Grace Hopper", 2021),
        ],
        NO_REFERENCE: [CommitFacts(frozenset({"jobs"}), "Ada Lovelace", 2015)],
    }
    summary = summarise(groups)
    text = json.dumps(summary)
    assert "Ada" not in text and "Grace" not in text
    assert summary["groups"] == {LINKED: 2, NO_REFERENCE: 1}
    assert summary["authors"][LINKED] == {"distinct": 2, "top10_share": 1.0}
    assert summary["authors"]["top10_overlap"] == 1
    assert summary["years"] == {LINKED: {"2020": 1, "2021": 1}, NO_REFERENCE: {"2015": 1}}
    assert (summary["module_tvd"], summary["year_tvd"]) == (0.5, 1.0)


def test_an_empty_group_has_no_distance():
    summary = summarise({LINKED: [CommitFacts(frozenset({"jobs"}), "A", 2020)], NO_REFERENCE: []})
    assert summary["module_tvd"] is None and summary["year_tvd"] is None
    assert summary["authors"][NO_REFERENCE] == {"distinct": 0, "top10_share": None}
