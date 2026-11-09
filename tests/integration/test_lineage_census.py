"""Slice C3a: `pace measure lineage` on the fixture repository."""

from __future__ import annotations

import pytest
from lineage_fixture import SCOPE

from provenance.analysis.lineage_census import CensusRefused, census, render_json
from provenance.ingest.blame import Target
from provenance.ingest.blame import build as blame_build
from provenance.lineage.build import build as lineage_build

pytestmark = pytest.mark.db

CORE, UTIL = SCOPE
ALIVE = (
    (CORE, "multiply"),
    (CORE, "plus"),
    (UTIL, "helper"),
    (UTIL, "zero_a"),
    (UTIL, "zero_b"),
)
KINDS = ("function_rename", "cross_file_move", "file_rename", "any")


def _target(repo, functions=ALIVE) -> Target:
    return Target(
        pinned=repo.sha["c11"], questions=len(functions), functions=tuple(sorted(functions))
    )


def _built(repo, conn, repo_id):
    blame_build(conn, repo=repo.root, repo_id=repo_id, target=_target(repo))  # arm L, from C2
    lineage_build(conn, repo=repo.root, repo_id=repo_id)


def _census(repo, conn, repo_id, functions=ALIVE):
    return census(
        conn,
        repo=repo.root,
        repo_id=repo_id,
        target=_target(repo, functions),
        move_commit=repo.sha["c7"],
        group_size=2,
    )


def test_the_census_counts_identities_breaks_and_agreement(
    lineage_graph, writer_conn, test_repo_id
):
    _built(lineage_graph, writer_conn, test_repo_id)
    counted = _census(lineage_graph, writer_conn, test_repo_id)
    assert counted["identities"] == {"total": 6, "alive_at_pinned": 5, "deaths": 1}
    assert counted["versions"] == {
        "total": 15,
        "by_stage": {"birth": 6, "h1": 1, "h3": 1, "move": 1, "name": 6},
        "renames": 2,
        "cross_file_moves": 1,
        "file_renames": 4,
    }
    assert (counted["ambiguous_cases"], counted["refused_revisions"]) == (1, 1)
    breaks = counted["identity_breaks"]
    assert breaks["with_the_move"]["n"] == breaks["without_it"]["n"] == 5
    assert [breaks["with_the_move"][kind]["count"] for kind in KINDS] == [2, 1, 3, 3]
    assert [breaks["without_it"][kind]["count"] for kind in KINDS] == [2, 1, 0, 3]
    agreement = counted["agreement"]
    assert agreement["sample"] == {"moved_stops": 0, "moved_reaches": 2, "other": 2}
    assert 0.0 <= agreement["mean_f1"]["point"] <= 1.0
    assert agreement["mean_f1"]["n"] == 4
    assert agreement["verdict"] in {"meets", "misses"}
    assert agreement["threshold"] == 0.9


def test_the_census_renders_the_same_bytes_twice(lineage_graph, writer_conn, test_repo_id):
    _built(lineage_graph, writer_conn, test_repo_id)
    first = render_json(_census(lineage_graph, writer_conn, test_repo_id))
    assert render_json(_census(lineage_graph, writer_conn, test_repo_id)) == first


def test_the_census_refuses_before_a_build_and_for_a_dead_function(
    lineage_graph, writer_conn, test_repo_id
):
    with pytest.raises(CensusRefused, match="pace lineage build"):
        _census(lineage_graph, writer_conn, test_repo_id)
    _built(lineage_graph, writer_conn, test_repo_id)
    with pytest.raises(CensusRefused, match="no identity alive"):
        _census(lineage_graph, writer_conn, test_repo_id, functions=(*ALIVE, (UTIL, "unused")))
