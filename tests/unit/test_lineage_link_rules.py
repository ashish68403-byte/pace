"""Slice C3a: pass 2 of the lineage engine, one cross-file rule and lineage assembly."""

from __future__ import annotations

from provenance.lineage.link import link_moves
from provenance.lineage.match import Revision, Symbol, walk_file
from provenance.parse.normalize import hash_ladder, identity_hashes

HELPER = 'def helper():\n    return "help"\n'


def sym(name: str, source: str) -> Symbol:
    blind = identity_hashes(source)
    return Symbol(name, (1, 2), hash_ladder(source), blind.h1, blind.h3)


def rev(commit: str, topo: int, *symbols: Symbol, path: str) -> Revision:
    return Revision(commit, topo, path, {symbol.name: symbol for symbol in symbols})


def passes(*files):
    return {file_id: walk_file(file_id, revisions) for file_id, revisions in enumerate(files, 1)}


def moved_away(path: str = "a.py"):
    return [rev("c1", 1, sym("helper", HELPER), path=path), rev("c2", 2, path=path)]


def test_a_function_moved_to_another_file_in_one_commit_keeps_its_identity():
    linked = link_moves(passes(moved_away(), [rev("c2", 2, sym("helper", HELPER), path="b.py")]))
    (lineage,) = linked.lineages
    assert [(v.commit, v.stage, v.path) for v in lineage.versions] == [
        ("c1", "birth", "a.py"),
        ("c2", "move", "b.py"),
    ]
    last = lineage.versions[-1]
    assert (last.is_move, last.is_rename, lineage.death, linked.moves) == (True, False, None, 1)


def test_a_move_to_a_new_name_is_also_a_rename():
    assist = sym("assist", HELPER.replace("helper", "assist"))
    linked = link_moves(passes(moved_away(), [rev("c2", 2, assist, path="b.py")]))
    (lineage,) = linked.lineages
    assert lineage.versions[-1].is_rename and lineage.versions[-1].symbol.name == "assist"


def test_two_candidates_link_nothing():
    linked = link_moves(
        passes(
            moved_away(),
            [rev("c2", 2, sym("helper", HELPER), path="b.py")],
            [rev("c2", 2, sym("helper", HELPER), path="c.py")],
        )
    )
    assert (len(linked.lineages), linked.moves) == (3, 0)
    (ambiguity,) = linked.ambiguities
    assert (ambiguity.stage, len(ambiguity.vanished), len(ambiguity.appeared)) == ("move", 1, 2)


def test_a_birth_and_a_death_at_different_commits_are_not_linked():
    linked = link_moves(passes(moved_away(), [rev("c3", 3, sym("helper", HELPER), path="b.py")]))
    assert (len(linked.lineages), linked.moves) == (2, 0)


def test_lineages_come_out_in_birth_order_with_their_deaths():
    late = sym("late", HELPER.replace("helper", "late"))
    early = sym("early", HELPER.replace("helper", "early"))
    linked = link_moves(
        passes(
            [rev("c2", 2, late, path="b.py")],
            [rev("c1", 1, early, path="a.py"), rev("c3", 3, path="a.py")],
        )
    )
    assert [lineage.birth.symbol.name for lineage in linked.lineages] == ["early", "late"]
    assert [lineage.death for lineage in linked.lineages] == [("c3", 3), None]
