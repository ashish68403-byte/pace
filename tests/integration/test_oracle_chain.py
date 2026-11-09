"""Slice C3b: following one chain gives the identity a walk over every Python file gives."""

from __future__ import annotations

from provenance.ingest.git_walk import CatFileBatch
from provenance.lineage.history import read_history, revisions_reader
from provenance.lineage.link import link_moves
from provenance.lineage.match import walk_file
from provenance.lineage.scope import follow_chain

TARGETS = (
    ("app/views.py", "dispatch"),
    ("app/views.py", "View.render"),
    ("app/views.py", "View.title"),
    ("app/views.py", "outer"),
    ("app/views.py", "helper"),
    ("lib/new_name.py", "parse"),
)


def _shape(lineage):
    return [(v.commit, v.path, v.symbol.name, v.stage) for v in lineage.versions]


def test_following_the_chain_equals_walking_every_python_file(oracle_repo):
    history = read_history(oracle_repo.repo, oracle_repo.sha["c10"])
    python = sorted(path for path in history.files if path.endswith(".py"))
    followed = {}
    with CatFileBatch(oracle_repo.repo) as cat:
        read = revisions_reader(history, cat, {})
        linked = link_moves(
            {index: walk_file(index, read(path)) for index, path in enumerate(python)}
        )
        for path, name in TARGETS:
            (whole,) = [
                lineage
                for lineage in linked.lineages
                if lineage.death is None
                and lineage.fragments[-1].file_id == python.index(path)
                and lineage.last.symbol.name == name
            ]
            followed[name] = follow_chain(path, name, read, history.python_touched)
            assert _shape(followed[name].lineage) == _shape(whole), name
    by_sha = {sha: name for name, sha in oracle_repo.sha.items()}
    helper = followed["helper"]
    assert [(by_sha[v.commit], v.path, v.stage) for v in helper.lineage.versions] == [
        ("c1", "app/util.py", "birth"),
        ("c6", "app/views.py", "move"),
        ("c10", "app/views.py", "name"),
    ]
    assert helper.rounds == 3
