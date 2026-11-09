"""Slice C3a: pass 1 of the lineage engine, one file's revisions into identity fragments."""

from __future__ import annotations

from provenance.lineage.match import Revision, Symbol, walk_file
from provenance.parse.normalize import hash_ladder, identity_hashes

ADD = 'def add(a, b):\n    """Sum two numbers."""\n    return a + b\n'
SCALE = "def scale(values, k):\n    out = [v * k for v in values]\n    return out\n"


def sym(name: str, source: str) -> Symbol:
    blind = identity_hashes(source)
    return Symbol(name, (1, source.count("\n")), hash_ladder(source), blind.h1, blind.h3)


def rev(commit: str, topo: int, *symbols: Symbol, path: str = "p.py") -> Revision:
    return Revision(commit, topo, path, {symbol.name: symbol for symbol in symbols})


def stages(fragment):
    return [(version.commit, version.stage) for version in fragment.versions]


def test_a_comment_writes_no_version_and_a_docstring_writes_one():
    commented = ADD.replace("    return", "    # adds\n    return")
    redoc = commented.replace("Sum two", "Add two")
    walked = walk_file(
        1,
        [
            rev("c1", 1, sym("add", ADD)),
            rev("c2", 2, sym("add", commented)),
            rev("c3", 3, sym("add", redoc)),
        ],
    )
    (fragment,) = walked.fragments
    assert stages(fragment) == [("c1", "birth"), ("c3", "name")]
    assert fragment.versions[-1].is_body_change


def test_a_rename_with_an_unchanged_body_is_linked_at_h1():
    renamed = ADD.replace("def add(", "def plus(")
    walked = walk_file(1, [rev("c1", 1, sym("add", ADD)), rev("c2", 2, sym("plus", renamed))])
    (fragment,) = walked.fragments
    assert stages(fragment) == [("c1", "birth"), ("c2", "h1")]
    last = fragment.versions[-1]
    assert (last.is_rename, last.is_body_change, last.symbol.name) == (True, False, "plus")


def test_a_rename_with_renamed_locals_is_linked_at_h3():
    renamed = (
        "def multiply(items, factor):\n    res = [i * factor for i in items]\n    return res\n"
    )
    walked = walk_file(
        1, [rev("c1", 1, sym("scale", SCALE)), rev("c2", 2, sym("multiply", renamed))]
    )
    (fragment,) = walked.fragments
    assert stages(fragment) == [("c1", "birth"), ("c2", "h3")]
    assert fragment.versions[-1].is_body_change


def test_one_hash_shared_by_several_leftovers_links_nothing():
    one = "def one():\n    return 0\n"
    walked = walk_file(
        1,
        [
            rev("c1", 1, sym("one", one)),
            rev(
                "c2",
                2,
                sym("zero_a", one.replace("one", "zero_a")),
                sym("zero_b", one.replace("one", "zero_b")),
            ),
        ],
    )
    assert len(walked.fragments) == 3
    (ambiguity,) = walked.ambiguities
    found = (ambiguity.commit, ambiguity.stage, len(ambiguity.vanished), len(ambiguity.appeared))
    assert found == ("c2", "h1", 1, 2)
    assert walked.fragments[0].death == ("c2", 2)
    assert all(fragment.ambiguous_birth for fragment in walked.fragments[1:])


def test_a_refused_revision_keeps_the_last_state():
    refused = Revision("c2", 2, "p.py", None)
    walked = walk_file(1, [rev("c1", 1, sym("add", ADD)), refused, rev("c3", 3, sym("add", ADD))])
    (fragment,) = walked.fragments
    assert stages(fragment) == [("c1", "birth")]
    assert walked.refused == [refused]


def test_a_file_rename_writes_a_moved_version_without_a_body_change():
    walked = walk_file(
        1,
        [
            rev("c1", 1, sym("add", ADD), path="old/p.py"),
            rev("c2", 2, sym("add", ADD), path="new/p.py"),
        ],
    )
    (fragment,) = walked.fragments
    last = fragment.versions[-1]
    assert (last.stage, last.path, last.is_move, last.is_body_change) == (
        "name",
        "new/p.py",
        True,
        False,
    )


def test_a_deleted_file_kills_its_functions_and_a_readded_one_is_born_again():
    walked = walk_file(
        1, [rev("c1", 1, sym("add", ADD)), rev("c2", 2), rev("c3", 3, sym("add", ADD))]
    )
    first, second = walked.fragments
    assert first.death == ("c2", 2)
    assert stages(second) == [("c3", "birth")]
