"""Slice B3's symbols: what the chunker would index, and what changed (ADR-0003 §3)."""

from __future__ import annotations

from provenance.parse.chunker import chunk_source
from provenance.parse.symbols import diff_symbols, symbols

MODULE = (
    "LIMIT = 3\n\n\n"
    "def alpha():\n    return 1\n\n\n"
    "class B:\n    size = 1\n\n    def grow(self):\n        self.size += 1\n"
)
LICENCE_ONLY = (
    "# Licensed to the Apache Software Foundation (ASF) under one\n"
    "# or more contributor license agreements.\n"
)
UNPARSEABLE = "def (:\n  ))) ((( @@@ $$$ ??? !!!\n  ]]] [[[ }}} {{{\n"


def test_functions_and_methods_are_symbols_and_class_cards_are_not():
    found = symbols(MODULE, "pkg/m.py")
    assert found is not None
    assert sorted(found) == ["B.grow", "alpha"]
    assert found["alpha"].startswith("def alpha():")
    assert found["B.grow"].startswith("def grow(self):")


def test_an_oversized_function_is_rejoined_in_part_order():
    text = "def big():\n" + "".join(f"    x{i} = compute({i}, 'value {i}')\n" for i in range(200))
    fragments = [c for c in chunk_source(text, "pkg/big.py") if c.kind == "fragment"]
    assert len(fragments) > 1  # the case under test: the chunker did split it
    in_order = sorted(fragments, key=lambda c: c.part_index)
    assert symbols(text, "pkg/big.py") == {"big": "\n".join(c.content for c in in_order)}


def test_a_getter_and_its_setter_are_one_symbol():
    text = (
        "class C:\n    @property\n    def x(self):\n        return 1\n\n"
        "    @x.setter\n    def x(self, value):\n        pass\n"
    )
    found = symbols(text, "pkg/c.py")
    assert found is not None and list(found) == ["C.x"]
    assert "return 1" in found["C.x"] and "pass" in found["C.x"]


def test_a_file_with_no_code_has_no_symbols():
    assert symbols("", "pkg/__init__.py") == {}
    assert symbols(LICENCE_ONLY, "pkg/__init__.py") == {}
    assert symbols("VALUES = [1, 2, 3]\nTOTAL = sum(VALUES)\n", "pkg/b.py") == {}


def test_unparseable_text_is_unknown_not_empty():
    assert symbols(UNPARSEABLE, "pkg/bad.py") is None


def test_a_comment_or_a_reindent_is_not_a_change():
    before = {"f": "def f():\n    return 1"}
    assert diff_symbols(before, {"f": "def f():\n    # why one\n    return 1"}).changed == ()
    assert diff_symbols(before, {"f": "def f():\n        return 1"}).changed == ()


def test_a_new_return_value_is_a_change():
    diff = diff_symbols({"f": "def f():\n    return 1"}, {"f": "def f():\n    return 2"})
    assert diff.changed == ("f",)


def test_added_and_disappeared_names_are_sorted():
    before = {"b": "def b():\n    pass", "a": "def a():\n    pass"}
    after = {"b": "def b():\n    pass", "d": "def d():\n    pass", "c": "def c():\n    pass"}
    diff = diff_symbols(before, after)
    assert (diff.added, diff.changed, diff.disappeared) == (("c", "d"), (), ("a",))
