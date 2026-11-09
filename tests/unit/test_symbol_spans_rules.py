"""Slice C2: every function's whole line span, as blame and `git log -L` need it.

ADR-0005's addendum of 2026-11-09: a function's lines run from its first decorator to its
last line, and a getter and setter under one name give two ranges. A split function's
fragments cover body statements only, so the span comes from the whole definition.
"""

from __future__ import annotations

from provenance.parse.chunker import chunk_source
from provenance.parse.symbols import symbol_spans, symbols

HEADER = (
    "# Licensed to the Apache Software Foundation (ASF) under one\n"
    "# or more contributor license agreements.\n"
    "\n"
)
SOURCE = HEADER + (
    "import functools\n"
    "\n"
    "\n"
    "@functools.cache\n"
    "def scale(x):\n"
    "    return x * 2\n"
    "\n"
    "\n"
    "class Box:\n"
    "    @property\n"
    "    def size(self):\n"
    "        return self._size\n"
    "\n"
    "    @size.setter\n"
    "    def size(self, value):\n"
    "        self._size = value\n"
)
BIG = (
    "@decorate\n"
    "def big():\n"
    '    """Why big exists."""\n'
    + "".join(f"    value_{i} = {i}\n" for i in range(300))
    + "    return value_0\n"
)
UNPARSEABLE = "def (:\n  ))) ((( @@@ $$$ ??? !!!\n  ]]] [[[ }}} {{{\n"


def test_a_span_runs_from_the_first_decorator_to_the_last_line_of_the_real_file():
    lines = SOURCE.split("\n")
    assert symbol_spans(SOURCE, "pkg/calc.py")["scale"] == ((7, 9),)
    assert lines[7 - 1] == "@functools.cache"  # the licence header's three lines count
    assert lines[9 - 1] == "    return x * 2"


def test_a_getter_and_a_setter_give_two_spans_under_one_name():
    assert symbol_spans(SOURCE, "pkg/calc.py")["Box.size"] == ((13, 15), (17, 19))


def test_a_split_function_has_one_span_holding_its_signature_and_docstring():
    chunks = chunk_source(BIG, "pkg/big.py")
    assert {chunk.kind for chunk in chunks} == {"fragment"}  # the test does split it
    assert min(chunk.start_line for chunk in chunks) == 4  # fragments start after the docstring
    assert symbol_spans(BIG, "pkg/big.py") == {"big": ((1, 304),)}


def test_every_function_chunk_carries_its_whole_definition():
    for chunk in chunk_source(SOURCE, "pkg/calc.py") + chunk_source(BIG, "pkg/big.py"):
        if chunk.kind in {"function", "method", "fragment"}:
            assert chunk.definition_lines is not None
            first, last = chunk.definition_lines
            assert first <= chunk.start_line <= chunk.end_line <= last
        else:
            assert chunk.definition_lines is None


def test_spans_name_the_same_functions_as_symbols():
    for source, path in ((SOURCE, "pkg/calc.py"), (BIG, "pkg/big.py")):
        assert set(symbol_spans(source, path)) == set(symbols(source, path))


def test_no_code_and_unparseable_text_answer_as_symbols_does():
    assert symbol_spans(HEADER, "pkg/__init__.py") == {}
    assert symbol_spans(UNPARSEABLE, "pkg/bad.py") is None
