"""GraphWriter against pace-db, on the ten-commit fixture repository (see conftest)."""

from __future__ import annotations

from itertools import pairwise

import pytest

from provenance.ingest.git_walk import IngestInvariantError

pytestmark = pytest.mark.db

FIRST_PARENT = ("c1", "c2", "c3", "c4", "m5", "c6", "c7", "c8", "c9")


def test_a_full_walk_writes_every_first_parent_commit(history_repo, test_repo_id, ingest, query):
    writer = ingest()
    rows = query(
        "SELECT commit_sha, topo_order, is_merge, committed_at IS NOT NULL, insertions "
        "FROM commits WHERE repo_id = %s ORDER BY topo_order",
        test_repo_id,
    )
    assert [history_repo.name_of(r[0]) for r in rows] == list(FIRST_PARENT)
    assert [r[1] for r in rows] == list(range(1, 10))
    assert [r[2] for r in rows] == [name == "m5" for name in FIRST_PARENT]
    assert all(r[3] for r in rows)
    assert all(r[4] is None for r in rows)  # not measured -> NULL, never 0
    links = query(
        "SELECT child_sha, parent_index, parent_sha FROM commit_parents WHERE repo_id = %s",
        test_repo_id,
    )
    sha = history_repo.sha
    assert set(links) == {(sha[child], 0, sha[parent]) for parent, child in pairwise(FIRST_PARENT)}
    assert writer.report()["commit_parents"]["to_unwalked_parents"] == 1  # m5's second parent


def test_a_second_run_writes_nothing_and_saw_everything(test_repo_id, ingest, query):
    first = ingest().report()
    second = ingest().report()
    assert first["commits"]["inserted"] == 9
    for table in ("commits", "commit_parents"):
        assert second[table]["inserted"] == 0, table
        assert second[table]["already_present"] == first[table]["inserted"], table
    runs = query(
        "SELECT status::text, rows_inserted, rows_updated, rows_skipped "
        "FROM ingest_runs WHERE repo_id = %s ORDER BY run_id",
        test_repo_id,
    )
    assert [run[0] for run in runs] == ["succeeded", "succeeded"]
    assert runs[1][1] == 0
    assert runs[1][3] == runs[0][1] > 0  # "0 inserted" alone is what a no-op writer reports


def test_a_truncated_walk_links_only_what_exists(history_repo, test_repo_id, ingest, query):
    first = ingest(max_commits=3).report()
    topo = dict(
        query("SELECT commit_sha, topo_order FROM commits WHERE repo_id = %s", test_repo_id)
    )
    sha = history_repo.sha
    assert topo == {sha["c9"]: 9, sha["c8"]: 8, sha["c7"]: 7}
    assert first["commit_parents"]["inserted"] == 2
    assert first["commit_parents"]["to_unwalked_parents"] == 1  # c7's parent c6 was never walked
    full = ingest().report()
    assert full["commit_parents"]["inserted"] == 6
    assert full["commit_parents"]["already_present"] == 2


def test_a_rejected_commit_is_dead_lettered_and_its_batch_survives(
    history_repo, test_repo_id, ingest, query, migrated_database
):
    # Another commit already holds topo_order 3, so c3's insert breaks commits_topo_unique.
    migrated_database.execute(
        "INSERT INTO commits (repo_id, commit_sha, topo_order, authored_at, committed_at) "
        "VALUES (%s, %s, 3, now(), now())",
        (test_repo_id, "f" * 40),
    )
    writer = ingest()
    stored = {
        r[0] for r in query("SELECT commit_sha FROM commits WHERE repo_id = %s", test_repo_id)
    }
    sha = history_repo.sha
    assert sha["c3"] not in stored
    # batch_size=3 puts c3, c2 and c1 in one batch: the savepoint is what saves c2 and c1.
    assert {sha[name] for name in FIRST_PARENT if name != "c3"} <= stored
    assert query(
        "SELECT commit_sha, error_class FROM dead_letters WHERE repo_id = %s", test_repo_id
    ) == [(sha["c3"], "UniqueViolation")]
    report = writer.report()
    assert report["commits"]["rejected"] == 1
    assert report["commit_parents"]["failed"] == 2  # c3 -> c2 and c4 -> c3


def test_a_stored_commit_with_another_topo_order_fails_the_run(
    history_repo, test_repo_id, ingest, query, migrated_database
):
    # An earlier run numbered c2 differently. ON CONFLICT DO NOTHING alone would keep it silently.
    migrated_database.execute(
        "INSERT INTO commits (repo_id, commit_sha, topo_order, authored_at, committed_at) "
        "VALUES (%s, %s, 99, now(), now())",
        (test_repo_id, history_repo.sha["c2"]),
    )
    with pytest.raises(IngestInvariantError, match="topo_order"):
        ingest()
    assert query("SELECT status::text FROM ingest_runs WHERE repo_id = %s", test_repo_id) == [
        ("failed",)
    ]
