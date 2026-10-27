"""The measurement end to end on the fixture repository, where every answer is known."""

from __future__ import annotations

import pytest

from provenance.analysis.truncation import NothingToMeasure, build_report, render_json

pytestmark = pytest.mark.db


def test_known_history_is_measured_exactly(history_repo, test_repo_id, ingest, migrated_database):
    ingest()
    report = build_report(migrated_database, test_repo_id, history_repo.root, history_repo.scope)
    files = {row["path"]: row for row in report["files"]}
    alpha, b = files["core/src/pkg/alpha.py"], files["core/src/pkg/b.py"]
    assert (alpha["n_current_path"], alpha["n_rename_aware"], alpha["n_follow"]) == (2, 5, 5)
    assert (alpha["n_hidden"], alpha["hidden_share"], alpha["n_hidden_with_pr_reference"]) == (
        3,
        0.6,
        1,
    )
    assert (alpha["follow_precision"], alpha["follow_recall"]) == (1.0, 1.0)
    assert (b["n_current_path"], b["n_rename_aware"], b["n_follow"]) == (3, 5, 5)
    assert (b["n_hidden"], b["hidden_share"], b["n_hidden_with_pr_reference"]) == (2, 0.4, 1)
    agg = report["aggregates"]
    assert agg["pooled_hidden_share"] == 0.5
    assert (agg["commits_touching_scope"], agg["commits_hidden_everywhere"]) == (8, 4)
    assert report["current_path_check"] == {"files_checked": 2, "files_failed": []}


def test_the_report_is_byte_identical_across_runs(
    history_repo, test_repo_id, ingest, migrated_database
):
    ingest()
    args = (migrated_database, test_repo_id, history_repo.root, history_repo.scope)
    assert render_json(build_report(*args)) == render_json(build_report(*args))


def test_an_empty_graph_is_refused(history_repo, test_repo_id, migrated_database):
    with pytest.raises(NothingToMeasure, match="run `pace ingest` first"):
        build_report(migrated_database, test_repo_id, history_repo.root, history_repo.scope)
