"""Slice C3a: name-blind h1 and h3 (ADR-0005's Errata of 2026-11-09).

The ladder's h1 and h3 include a definition's own name, so a pure rename would change both.
`identity_hashes` replaces every identifier equal to that name first: a rename, a recursive call
included, keeps both hashes.
"""

from __future__ import annotations

from provenance.parse.normalize import hash_ladder, identity_hashes

BASE = (
    "def total(values):\n"
    '    """Sum them."""\n'
    "    result = 0\n"
    "    for value in values:\n"
    "        result += value  # running\n"
    "    return result\n"
)
PAIR = (
    "@property\n"
    "def size(self):\n"
    "    return self._size\n"
    "\n"
    "@size.setter\n"
    "def size(self, value):\n"
    "    self._size = value\n"
)


def test_a_rename_keeps_both_name_blind_hashes_and_the_ordinary_h1_sees_it():
    renamed = BASE.replace("def total(", "def sum_all(")
    assert identity_hashes(renamed) == identity_hashes(BASE)
    assert hash_ladder(renamed).h1 != hash_ladder(BASE).h1


def test_a_recursive_call_is_blind_too():
    recursive = "def fact(n):\n    return 1 if n < 2 else n * fact(n - 1)\n"
    assert identity_hashes(recursive.replace("fact", "factorial")) == identity_hashes(recursive)


def test_renaming_a_local_keeps_h3_but_not_h1():
    local = BASE.replace("result", "acc")
    assert identity_hashes(local).h3 == identity_hashes(BASE).h3
    assert identity_hashes(local).h1 != identity_hashes(BASE).h1


def test_a_comment_changes_neither_and_a_docstring_changes_h1():
    assert identity_hashes(BASE.replace("# running", "# one more")) == identity_hashes(BASE)
    assert identity_hashes(BASE.replace("Sum them.", "Add them.")).h1 != identity_hashes(BASE).h1


def test_a_getter_and_setter_pair_is_blind_to_its_shared_name():
    renamed = PAIR.replace("def size(", "def extent(").replace("@size.", "@extent.")
    assert identity_hashes(renamed) == identity_hashes(PAIR)
