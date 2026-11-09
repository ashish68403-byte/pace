"""Slice C2: the build stores both owners and `git log -L` for each function, and resumes."""

from __future__ import annotations

import pytest
from blame_fixture import OLD_PATH, PATH, SPANS

from provenance.ingest.blame import BlameRefused, Target, build

pytestmark = pytest.mark.db


def _target(blame_repo, names=tuple(SPANS)) -> Target:
    functions = tuple((PATH, name) for name in sorted(names))
    return Target(pinned=blame_repo.sha["c6"], questions=len(functions), functions=functions)


def _owners(query, repo_id):
    rows = query(
        "SELECT lower(line_range), upper(line_range), orig_commit_sha, orig_path, "
        "effective_commit_sha FROM blame_ranges WHERE repo_id = %s",
        repo_id,
    )
    return {
        line: (orig, path, effective)
        for low, high, orig, path, effective in rows
        for line in range(low, high)
    }


def _count(query, table, repo_id):
    return query(f"SELECT count(*) FROM {table} WHERE repo_id = %s", repo_id)[0][0]


def test_the_build_stores_both_owners_of_every_line(blame_graph, writer_conn, test_repo_id, query):
    sha = blame_graph.sha
    result = build(
        writer_conn, repo=blame_graph.root, repo_id=test_repo_id, target=_target(blame_graph)
    )
    owners = _owners(query, test_repo_id)
    assert (result.files_blamed, result.functions_logged) == (1, 3)
    assert set(owners) == {*range(4, 10), *range(12, 15), *range(18, 21), *range(22, 25)}
    assert owners[5] == (sha["c5"], PATH, sha["c1"])  # the mechanical commit changed it
    assert owners[6] == (sha["c5"], PATH, None)  # it added this one, matching no line
    assert owners[9] == (sha["m3"], OLD_PATH, sha["m3"])  # the side branch's line, via its merge
    assert owners[14] == (sha["c5"], PATH, sha["c1"])
    assert owners[7] == (sha["c1"], OLD_PATH, sha["c1"])
    assert sha["s1"] not in {orig for orig, _, _ in owners.values()}


def test_each_functions_line_log_is_stored(blame_graph, writer_conn, test_repo_id, query):
    sha = blame_graph.sha
    build(writer_conn, repo=blame_graph.root, repo_id=test_repo_id, target=_target(blame_graph))
    logged: dict[str, set[str]] = {}
    for name, commit in query(
        "SELECT qualified_name, commit_sha FROM line_log_commits WHERE repo_id = %s", test_repo_id
    ):
        logged.setdefault(name, set()).add(commit)
    assert logged == {
        "total": {sha["c1"], sha["m3"], sha["c5"], sha["c6"]},
        "scale": {sha["c1"], sha["c5"]},
        "Box.size": {sha["c1"]},
    }


def test_a_second_build_writes_nothing(blame_graph, writer_conn, test_repo_id, query):
    target = _target(blame_graph)
    build(writer_conn, repo=blame_graph.root, repo_id=test_repo_id, target=target)
    before = (
        _count(query, "blame_ranges", test_repo_id),
        _count(query, "line_log_commits", test_repo_id),
    )
    again = build(writer_conn, repo=blame_graph.root, repo_id=test_repo_id, target=target)
    assert (again.files_blamed, again.files_skipped) == (0, 1)
    assert (again.functions_logged, again.functions_skipped) == (0, 3)
    after = (
        _count(query, "blame_ranges", test_repo_id),
        _count(query, "line_log_commits", test_repo_id),
    )
    assert after == before


def test_a_function_missing_at_the_pinned_commit_stops_the_build(
    blame_graph, writer_conn, test_repo_id, query
):
    target = _target(blame_graph, names=("nope", "total"))
    with pytest.raises(BlameRefused, match="'nope' is not a function"):
        build(writer_conn, repo=blame_graph.root, repo_id=test_repo_id, target=target)
    assert _count(query, "blame_ranges", test_repo_id) == 0


def test_an_empty_mechanical_list_stops_the_build(
    blame_graph, writer_conn, test_repo_id, migrated_database
):
    migrated_database.execute(
        "UPDATE commits SET is_mechanical = false WHERE repo_id = %s", (test_repo_id,)
    )
    with pytest.raises(BlameRefused, match="no commit is marked mechanical"):
        build(writer_conn, repo=blame_graph.root, repo_id=test_repo_id, target=_target(blame_graph))


def test_rows_another_command_made_are_refused_unless_rebuilt(
    blame_graph, writer_conn, test_repo_id, migrated_database, query
):
    target = _target(blame_graph)
    build(writer_conn, repo=blame_graph.root, repo_id=test_repo_id, target=target)
    migrated_database.execute(
        "UPDATE blame_ranges SET blame_algo = 'another command' WHERE repo_id = %s",
        (test_repo_id,),
    )
    with pytest.raises(BlameRefused, match="another command"):
        build(writer_conn, repo=blame_graph.root, repo_id=test_repo_id, target=target)
    rebuilt = build(
        writer_conn, repo=blame_graph.root, repo_id=test_repo_id, target=target, rebuild=True
    )
    assert rebuilt.files_blamed == 1
    algos = query("SELECT DISTINCT blame_algo FROM blame_ranges WHERE repo_id = %s", test_repo_id)
    assert [algo.startswith("git ") for (algo,) in algos] == [True]
