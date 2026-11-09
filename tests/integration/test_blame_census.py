"""Slice C2: `pace measure blame` counts what the build stored, and the move, without gold."""

from __future__ import annotations

import pytest
from blame_fixture import PATH, SPANS

from provenance.analysis.blame_census import CensusRefused, census, render_json
from provenance.ingest.blame import Target, build

pytestmark = pytest.mark.db


def _target(blame_repo) -> Target:
    functions = tuple((PATH, name) for name in sorted(SPANS))
    return Target(pinned=blame_repo.sha["c6"], questions=3, functions=functions)


def _census(blame_repo, conn, repo_id):
    return census(
        conn,
        repo=blame_repo.root,
        repo_id=repo_id,
        target=_target(blame_repo),
        move_commit=blame_repo.sha["c4"],
    )


def test_the_census_counts_lines_arms_and_the_move(blame_graph, writer_conn, test_repo_id):
    build(writer_conn, repo=blame_graph.root, repo_id=test_repo_id, target=_target(blame_graph))
    counted = _census(blame_graph, writer_conn, test_repo_id)
    assert counted["lines"] == {
        "total": 15,
        "plain_owner_mechanical": 3,  # lines 5, 6 and 14
        "plain_owner_mechanical_share": 0.2,
        "filtered_owner_null": 1,  # line 6, which the mechanical commit added
        "filtered_owner_null_share": 0.0667,
    }
    per_function = counted["commits_per_function"]
    assert per_function["arm_a"]["max"] == 3  # total: c1, c6, m3
    assert per_function["blame_unfiltered"]["max"] == 4  # total: c1, c5, c6, m3
    assert per_function["arm_l"]["max"] == 3  # total's c1, m3, c6: c5 is mechanical
    assert counted["move"] == {
        "commit": blame_graph.sha["c4"],
        "question_files_the_move_renamed": 1,
        "functions_in_those_files": 3,
        "existed_before_the_move": 3,
        "line_log_reaches_before_the_move": 3,
        "blame_owner_at_the_old_path": 3,
    }


def test_the_census_renders_the_same_bytes_twice(blame_graph, writer_conn, test_repo_id):
    build(writer_conn, repo=blame_graph.root, repo_id=test_repo_id, target=_target(blame_graph))
    first = render_json(_census(blame_graph, writer_conn, test_repo_id))
    assert render_json(_census(blame_graph, writer_conn, test_repo_id)) == first


def test_the_census_refuses_before_the_build(blame_graph, writer_conn, test_repo_id):
    with pytest.raises(CensusRefused, match="pace blame build"):
        _census(blame_graph, writer_conn, test_repo_id)
