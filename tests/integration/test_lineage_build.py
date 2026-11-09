"""Slice C3a: the build stores the fixture's identities, its ambiguity and its refused revision."""

from __future__ import annotations

import pytest

from provenance.lineage.build import build

pytestmark = pytest.mark.db

_ROWS = (
    "SELECT l.lineage_id, l.death_commit, l.current_qualified_name, v.commit_sha, v.match_stage, "
    "v.path, v.qualified_name, v.is_rename, v.is_move, v.is_body_change "
    "FROM symbol_lineage l JOIN symbol_versions v USING (lineage_id) "
    "WHERE l.repo_id = %s ORDER BY l.lineage_id, v.commit_topo_order"
)
_EDGES = (
    "SELECT src.birth_commit, dst.birth_commit, e.at_commit, e.confidence, e.evidence->>'stage' "
    "FROM lineage_edges e JOIN symbol_lineage src ON src.lineage_id = e.src_lineage_id "
    "JOIN symbol_lineage dst ON dst.lineage_id = e.dst_lineage_id "
    "WHERE e.repo_id = %s AND e.kind = 'ambiguous'"
)
_DEAD = "SELECT subject_ref, commit_sha FROM dead_letters WHERE repo_id = %s AND stage = 'lineage'"


def _lineages(query, repo_id, sha):
    names = {value: key for key, value in sha.items()}
    found: dict[int, dict] = {}
    for lid, death, current, commit, stage, path, name, ren, mov, body in query(_ROWS, repo_id):
        entry = found.setdefault(
            lid, {"death": names.get(death), "current": current, "versions": []}
        )
        entry["versions"].append((names[commit], stage, path, name, ren, mov, body))
    return {entry["current"] or entry["versions"][0][3]: entry for entry in found.values()}


def test_the_fixtures_identities(lineage_graph, writer_conn, test_repo_id, query):
    stats = build(writer_conn, repo=lineage_graph.root, repo_id=test_repo_id)
    found = _lineages(query, test_repo_id, lineage_graph.sha)
    counted = (stats.files, stats.revisions, stats.refused, stats.lineages, stats.moves)
    assert counted + (stats.ambiguous,) == (2, 14, 1, 6, 1, 1)
    assert found["plus"]["versions"] == [
        ("c1", "birth", "pkg/core.py", "add", False, False, False),
        ("c3", "name", "pkg/core.py", "add", False, False, True),
        ("c4", "h1", "pkg/core.py", "plus", True, False, False),
        ("c7", "name", "src/pkg/core.py", "plus", False, True, False),
        ("c8", "name", "src/pkg/core.py", "plus", False, False, True),
    ]
    assert found["multiply"]["versions"] == [
        ("c1", "birth", "pkg/core.py", "scale", False, False, False),
        ("c5", "h3", "pkg/core.py", "multiply", True, False, True),
        ("c7", "name", "src/pkg/core.py", "multiply", False, True, False),
    ]
    assert found["helper"]["versions"] == [
        ("c1", "birth", "pkg/core.py", "helper", False, False, False),
        ("c6", "move", "pkg/util.py", "helper", False, True, False),
        ("c7", "name", "src/pkg/util.py", "helper", False, True, False),
    ]
    assert found["unused"]["death"] == "c9"
    assert [version[0] for version in found["unused"]["versions"]] == ["c1", "c7"]
    assert found["zero_a"]["versions"] == [
        ("c9", "birth", "src/pkg/util.py", "zero_a", False, False, False)
    ]


def test_the_ambiguity_and_the_refused_revision_are_stored(
    lineage_graph, writer_conn, test_repo_id, query
):
    build(writer_conn, repo=lineage_graph.root, repo_id=test_repo_id)
    names = {value: key for key, value in lineage_graph.sha.items()}
    edges = query(_EDGES, test_repo_id)
    assert sorted(
        (names[a], names[b], names[c], conf, stage) for a, b, c, conf, stage in edges
    ) == [
        ("c1", "c9", "c9", 0.5, "h1"),
        ("c1", "c9", "c9", 0.5, "h1"),
    ]
    dead = query(_DEAD, test_repo_id)
    assert [(path, names[commit]) for path, commit in dead] == [("src/pkg/core.py", "c10")]


def test_two_builds_store_the_same_identities(lineage_graph, writer_conn, test_repo_id, query):
    build(writer_conn, repo=lineage_graph.root, repo_id=test_repo_id)
    first = _lineages(query, test_repo_id, lineage_graph.sha)
    build(writer_conn, repo=lineage_graph.root, repo_id=test_repo_id)
    assert _lineages(query, test_repo_id, lineage_graph.sha) == first
    assert len(query(_DEAD, test_repo_id)) == 1


def test_a_mechanical_commit_keeps_its_version(lineage_graph, writer_conn, test_repo_id, query):
    build(writer_conn, repo=lineage_graph.root, repo_id=test_repo_id)
    ((mechanical,),) = query(
        "SELECT is_mechanical FROM commits WHERE repo_id = %s AND commit_sha = %s",
        test_repo_id,
        lineage_graph.sha["c8"],
    )
    assert mechanical
    plus = _lineages(query, test_repo_id, lineage_graph.sha)["plus"]["versions"]
    assert ("c8", "name") in [(version[0], version[1]) for version in plus]
