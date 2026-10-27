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
    assert first["files"]["inserted"] == 2
    for table in ("commits", "commit_parents", "files", "file_revisions"):
        assert second[table]["inserted"] == 0, table
        assert second[table]["already_present"] == first[table]["inserted"], table
    assert first["files"]["spans_updated"] == 2
    assert second["files"]["spans_updated"] == 0
    runs = query(
        "SELECT status::text, rows_inserted, rows_updated, rows_skipped "
        "FROM ingest_runs WHERE repo_id = %s ORDER BY run_id",
        test_repo_id,
    )
    assert [run[0] for run in runs] == ["succeeded", "succeeded"]
    assert (runs[1][1], runs[1][2]) == (0, 0)
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


def _history(query, repo_id, path):
    return query(
        "SELECT r.commit_sha, r.change_type::text, r.old_path, r.path_at_commit, r.similarity "
        "FROM file_revisions AS r JOIN files AS f ON f.file_id = r.file_id "
        "WHERE f.repo_id = %s AND f.path = %s ORDER BY r.commit_topo_order",
        repo_id,
        path,
    )


def test_history_follows_a_file_through_two_renames(history_repo, test_repo_id, ingest, query):
    ingest()
    sha = history_repo.sha
    assert _history(query, test_repo_id, "core/src/pkg/alpha.py") == [
        (sha["c1"], "added", None, "pkg/a.py", None),
        (sha["c2"], "modified", None, "pkg/a.py", None),
        (sha["c3"], "renamed", "pkg/a.py", "pkg/alpha.py", 100),
        (sha["c6"], "renamed", "pkg/alpha.py", "core/src/pkg/alpha.py", 100),
        (sha["c7"], "modified", None, "core/src/pkg/alpha.py", None),
    ]


def test_a_deletion_keeps_the_last_content_and_a_re_add_continues_the_file(
    history_repo, test_repo_id, ingest, query
):
    ingest()
    sha = history_repo.sha
    rows = _history(query, test_repo_id, "core/src/pkg/b.py")
    assert [(history_repo.name_of(r[0]), r[1]) for r in rows] == [
        ("c1", "added"),
        ("m5", "modified"),  # s1's change, through the merge's first-parent diff
        ("c6", "renamed"),
        ("c8", "deleted"),
        ("c9", "added"),
    ]
    assert query(
        "SELECT blob_sha FROM file_revisions WHERE repo_id = %s AND commit_sha = %s",
        test_repo_id,
        sha["c8"],
    ) == [(history_repo.blob(sha["c7"], "core/src/pkg/b.py"),)]


def test_each_file_spans_its_first_and_last_commit(history_repo, test_repo_id, ingest, query):
    ingest()
    sha = history_repo.sha
    spans = query(
        "SELECT path, first_seen_commit, last_seen_commit FROM files WHERE repo_id = %s",
        test_repo_id,
    )
    assert {path: (first, last) for path, first, last in spans} == {
        "core/src/pkg/alpha.py": (sha["c1"], sha["c7"]),
        "core/src/pkg/b.py": (sha["c1"], sha["c9"]),
    }


def test_a_scope_path_with_no_history_fails_a_complete_walk(history_repo, ingest):
    with pytest.raises(IngestInvariantError, match="no files row"):
        ingest(scope=[*history_repo.scope, "core/src/pkg/ghost.py"])


def test_an_in_scope_file_that_ends_deleted_fails_the_walk(history_repo, ingest):
    # Walking only up to c8 leaves b.py's newest revision a deletion, which scope rules out.
    with pytest.raises(IngestInvariantError, match="end in a deletion"):
        ingest(rev=history_repo.sha["c8"])
