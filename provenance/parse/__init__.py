"""Parsing layer: tree-sitter based chunking and the function-identity hash ladder.

Why tree-sitter and not :mod:`ast`:

* 5.5% of 2015-era Airflow files do not parse under Python 3.12 (``async`` used as
  an identifier, ``print`` statements in vendored code, py2 ``except X, e``).
  :func:`ast.parse` raises and the file is lost; tree-sitter is error tolerant and
  returns a tree with ERROR nodes, so we keep the parseable regions and
  dead-letter only what is genuinely unusable.
* ``ast.dump`` is NOT stable across Python minor versions (3.13+ omits fields left
  at their defaults, 3.12 emits them all), so identical source hashes differently
  depending on which interpreter ran the ingest. The tree-sitter s-expression is
  pinned by the grammar version instead, which we record explicitly.

This module owns the single parser factory so the grammar version reported by
:mod:`provenance.parse.normalize` is the one that actually produced every tree.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import metadata
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

    from tree_sitter import Language, Node, Parser, Tree

__all__ = [
    "get_language",
    "get_parser",
    "parse_source",
    "grammar_version",
    "tree_sitter_version",
    "iter_nodes",
    "node_text",
]


@lru_cache(maxsize=1)
def get_language() -> Language:
    """Return the compiled tree-sitter Python language (cached process-wide)."""
    import tree_sitter_python as ts_python
    from tree_sitter import Language

    return Language(ts_python.language())


@lru_cache(maxsize=1)
def _parser_takes_language() -> bool:
    """tree-sitter >=0.23 takes the language in the constructor; 0.21/0.22 use a setter."""
    from tree_sitter import Parser

    try:
        Parser(get_language())
    except TypeError:
        return False
    return True


def get_parser() -> Parser:
    """Build a fresh parser. Parsers are cheap but NOT thread-safe, so never cache one."""
    from tree_sitter import Parser

    if _parser_takes_language():
        return Parser(get_language())
    parser = Parser()
    parser.set_language(get_language())  # type: ignore[attr-defined]
    return parser


def parse_source(source: str | bytes) -> Tree:
    """Parse UTF-8 source into a tree-sitter tree. Never raises on bad syntax."""
    data = source.encode("utf-8") if isinstance(source, str) else source
    return get_parser().parse(data)


@lru_cache(maxsize=1)
def grammar_version() -> str:
    """Version of the *grammar* package. Part of ``ast_norm_version``."""
    try:
        return metadata.version("tree-sitter-python")
    except metadata.PackageNotFoundError:  # pragma: no cover - source checkout
        return "unknown"


@lru_cache(maxsize=1)
def tree_sitter_version() -> str:
    """Version of the tree-sitter runtime binding."""
    try:
        return metadata.version("tree-sitter")
    except metadata.PackageNotFoundError:  # pragma: no cover - source checkout
        return "unknown"


def node_text(node: Node, source: bytes) -> str:
    """Decode a node's byte span. Airflow has non-UTF8-clean files; never hard-fail."""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def iter_nodes(node: Node, *, named_only: bool = False) -> Iterator[Node]:
    """Depth-first pre-order walk. Iterative: some Airflow files nest ~200 deep."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        children = current.named_children if named_only else current.children
        stack.extend(reversed(children))
