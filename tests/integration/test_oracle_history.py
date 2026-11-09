"""Slice C3b: histories rebuilt in memory equal what checkpoint A stores, and pass 1 over them."""

from __future__ import annotations

from provenance.ingest.git_walk import CatFileBatch
from provenance.lineage.build import file_passes
from provenance.lineage.history import read_history, revisions_reader
from provenance.lineage.match import walk_file

_ROWS = (
    "SELECT f.path, r.commit_sha, r.commit_topo_order, r.change_type::text, r.blob_sha, "
    "r.path_at_commit FROM file_revisions r JOIN files f USING (file_id) "
    "WHERE r.repo_id = %s ORDER BY f.path, r.commit_topo_order"
)
_PATHS = "SELECT file_id, path FROM files WHERE repo_id = %s"
TOUCHED = {
    "c1": ("app/util.py", "app/views.py", "lib/new_name.py"),
    "c2": ("app/views.py",),
    "c4": ("app/views.py",),
    "m5": ("app/views.py",),
    "c6": ("app/util.py", "app/views.py"),
    "c7": ("lib/new_name.py",),
    "c8": ("app/views.py",),
    "c9": ("app/views.py",),
    "c10": ("app/views.py",),
}


def test_the_history_equals_the_rows_the_ingest_writes(oracle_graph, test_repo_id, query):
    history = read_history(oracle_graph.repo, oracle_graph.sha["c10"])
    walked = [
        (path, found.commit, found.topo, found.change_type, found.blob, found.path)
        for path, revisions in history.files.items()
        for found in revisions
    ]
    assert walked == [tuple(row) for row in query(_ROWS, test_repo_id)]
    assert history.rejected == ()
    by_sha = {sha: name for name, sha in oracle_graph.sha.items()}
    assert {by_sha[commit]: paths for commit, paths in history.python_touched.items()} == TOUCHED


def _shape(walked):
    fragments = [
        (
            [
                (v.commit, v.topo, v.path, v.symbol.name, v.stage, v.is_rename, v.is_move)
                + (v.is_body_change,)
                for v in fragment.versions
            ],
            fragment.death,
        )
        for fragment in walked.fragments
    ]
    return fragments, len(walked.refused), len(walked.ambiguities)


def test_pass_one_over_it_equals_the_builds(oracle_graph, writer_conn, test_repo_id, query):
    stored, _ = file_passes(writer_conn, oracle_graph.repo, test_repo_id)
    paths = dict(query(_PATHS, test_repo_id))
    history = read_history(oracle_graph.repo, oracle_graph.sha["c10"])
    assert sorted(paths.values()) == sorted(history.files)
    with CatFileBatch(oracle_graph.repo) as cat:
        read = revisions_reader(history, cat, {})
        for file_id, found in stored.items():
            assert _shape(walk_file(file_id, read(paths[file_id]))) == _shape(found)
