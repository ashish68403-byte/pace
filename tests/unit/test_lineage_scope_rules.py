"""Slice C3b: following one function's chain across files (ADR-0005's addendum of 2026-11-09)."""

from __future__ import annotations

import pytest

from provenance.lineage.match import Revision, Symbol
from provenance.lineage.scope import ChainRefused, follow_chain
from provenance.parse.normalize import hash_ladder, identity_hashes

HELPER = "def helper(text):\n    return text.strip()\n"
KEEP = "def keep():\n    return 1\n"
OTHER = "def other():\n    return 2\n"


def sym(name: str, source: str) -> Symbol:
    blind = identity_hashes(source)
    return Symbol(name, (1, source.count("\n")), hash_ladder(source), blind.h1, blind.h3)


def rev(commit: str, topo: int, path: str, *symbols: Symbol) -> Revision:
    return Revision(commit, topo, path, {symbol.name: symbol for symbol in symbols})


def reader(files, calls):
    def read(path):
        calls.append(path)
        return files[path]

    return read


def stages(followed):
    return [(v.commit, v.path, v.stage) for v in followed.lineage.versions]


def test_a_move_in_from_an_unwalked_file_is_followed():
    files = {
        "a.py": [
            rev("c1", 1, "a.py", sym("helper", HELPER), sym("keep", KEEP)),
            rev("c3", 3, "a.py", sym("keep", KEEP)),
        ],
        "b.py": [
            rev("c2", 2, "b.py", sym("other", OTHER)),
            rev("c3", 3, "b.py", sym("other", OTHER), sym("helper", HELPER)),
        ],
    }
    touched = {"c1": ("a.py",), "c2": ("b.py",), "c3": ("a.py", "b.py")}
    calls: list[str] = []
    followed = follow_chain("b.py", "helper", reader(files, calls), touched)
    assert stages(followed) == [("c1", "a.py", "birth"), ("c3", "b.py", "move")]
    assert followed.files == ("a.py", "b.py")
    assert followed.rounds == 2
    assert sorted(calls) == ["a.py", "b.py"]  # each file is read once


def test_a_second_candidate_leaves_the_move_unlinked():
    files = {
        "a.py": [
            rev("c1", 1, "a.py", sym("helper", HELPER), sym("keep", KEEP)),
            rev("c3", 3, "a.py", sym("keep", KEEP)),
        ],
        "b.py": [
            rev("c2", 2, "b.py", sym("other", OTHER)),
            rev("c3", 3, "b.py", sym("other", OTHER), sym("helper", HELPER)),
        ],
        "c.py": [
            rev("c1", 1, "c.py", sym("helper", HELPER), sym("other", OTHER)),
            rev("c3", 3, "c.py", sym("other", OTHER)),
        ],
    }
    touched = {"c1": ("a.py", "c.py"), "c2": ("b.py",), "c3": ("a.py", "b.py", "c.py")}
    followed = follow_chain("b.py", "helper", reader(files, []), touched)
    assert stages(followed) == [("c3", "b.py", "birth")]
    assert followed.files == ("a.py", "b.py", "c.py")


def test_no_identity_alive_under_the_name_is_refused():
    files = {"a.py": [rev("c1", 1, "a.py", sym("keep", KEEP))]}
    with pytest.raises(ChainRefused):
        follow_chain("a.py", "helper", reader(files, []), {"c1": ("a.py",)})
