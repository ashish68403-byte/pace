"""A Python file's functions and methods by qualified name, and what one revision did to them.

Slice B3's generator (docs/tasks/eval-harness/003_golden_set_generator.md) asks about a
function or method a commit added or changed (ADR-0003 §3). Both come from the chunker,
so a symbol here is exactly what the chunker indexes:

* a function (`alpha`) or a method (`B.grow`). An oversized function's fragments share
  one qualified name and are rejoined in the order the chunker emits them, which is part
  order; a property's getter and setter share one name and are joined the same way;
* never a class card -- a class "changes" whenever one of its methods does -- and never
  module-level code.

*Changed* means the comment- and layout-blind token stream differs
(`normalize.hash_ladder(...).h1`): a new comment or a re-indent is not a change, a new
return value or docstring is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from provenance.graph.ids import normalize_content
from provenance.ingest.scope import strip_licence_header
from provenance.parse.chunker import ChunkParseError, chunk_source
from provenance.parse.normalize import hash_ladder

__all__ = ["SYMBOL_KINDS", "SymbolDiff", "diff_symbols", "symbols", "tokens_differ"]

#: The chunk kinds that are a symbol's own text. "class" (a synthesized card) and
#: "module" are not.
SYMBOL_KINDS: Final = frozenset({"function", "method", "fragment"})


def symbols(text: str, path: str) -> dict[str, str] | None:
    """`{qualified_name: text}` for every function and method in one file's text.

    `{}` when the file holds no code (empty, or a licence header alone). `None` when the
    chunker refuses the text (Python 2, or mostly parse errors), so a caller can tell
    "no symbols" from "unknown". The chunker raises the same error for all three.
    """
    try:
        chunks = chunk_source(text, path)
    except ChunkParseError:
        body, _ = strip_licence_header(text)
        return {} if not body.strip() else None
    parts: dict[str, list[str]] = {}
    for chunk in chunks:
        name = chunk.qualified_name
        if chunk.kind in SYMBOL_KINDS and name and "<anonymous>" not in name:
            parts.setdefault(name, []).append(chunk.content)
    return {name: "\n".join(texts) for name, texts in parts.items()}


def tokens_differ(before: str, after: str) -> bool:
    """True when the token stream differs. Comments and layout never count."""
    if normalize_content(before) == normalize_content(after):
        return False  # the cheap case: the same text
    return hash_ladder(before).h1 != hash_ladder(after).h1


@dataclass(frozen=True)
class SymbolDiff:
    """What one revision did to a file's symbols. Each name list is sorted."""

    added: tuple[str, ...]
    changed: tuple[str, ...]
    disappeared: tuple[str, ...]


def diff_symbols(before: dict[str, str], after: dict[str, str]) -> SymbolDiff:
    """Names only after; on both sides with different tokens; only before."""
    common = before.keys() & after.keys()
    return SymbolDiff(
        added=tuple(sorted(after.keys() - before.keys())),
        changed=tuple(sorted(name for name in common if tokens_differ(before[name], after[name]))),
        disappeared=tuple(sorted(before.keys() - after.keys())),
    )
