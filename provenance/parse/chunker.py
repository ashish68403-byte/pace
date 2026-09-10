"""AST-bounded chunking with tree-sitter.

Rules, and the failure each one prevents
----------------------------------------
* **Split only at function / method / class boundaries.** A fixed-window splitter
  cuts a function in half; the half containing the ``if`` and the half containing
  the comment that explains the ``if`` then land in different chunks and neither
  answers the question.
* **Never split a function.** If one genuinely exceeds the token budget (Airflow's
  ``TaskInstance`` methods do), split at *statement* boundaries and prepend the
  signature and docstring to every fragment, so each fragment still says what it
  belongs to and is retrievable on its own.
* **Strip the Apache licence header before hashing.** ~16 identical lines x 605
  files = ~9,700 lines of boilerplate. Hash it and small chunks collide; embed it
  and every query matches the same dense, meaningless neighbourhood.
* **tree-sitter, not :mod:`ast`.** 5.5% of 2015-era Airflow files raise
  ``SyntaxError`` under Python 3.12. tree-sitter is error tolerant, so we keep the
  parseable regions of those files and dead-letter only what is truly unusable —
  never silently drop a file.
* **Emit the underscore-free joined form in ``lexical_tokens``.** Measured:
  ``'_normalise_url'::pdb.source_code::text[]`` -> ``{normalise,url}``. The whole
  identifier does NOT survive tokenisation, so a query for ``normaliseurl`` (or a
  reranker keying on the joined form) finds nothing unless we emit it ourselves.
  The exact form is covered separately by the ``content::pdb.literal`` index.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from provenance.config import settings
from provenance.graph.ids import chunk_id
from provenance.ingest.scope import strip_licence_header
from provenance.parse import node_text, parse_source

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

__all__ = [
    "Chunk",
    "ChunkParseError",
    "MAX_CHUNK_TOKENS",
    "chunk_source",
    "chunk_file",
    "lexical_tokens",
    "estimate_tokens",
]

# Sized for bge-small (512 tokens) with room for the qualified-name prefix the
# embedder prepends. Chunks are allowed to exceed it only when a single statement
# does; we never cut mid-statement.
MAX_CHUNK_TOKENS = 400

# A file whose parse is more ERROR than code is not "partially recovered", it is
# a different language (Jinja templates, py2 with `print` statements everywhere).
MAX_ERROR_BYTE_RATIO = 0.35

_DEF_TYPES = frozenset({"function_definition", "class_definition", "decorated_definition"})
_IMPORT_TYPES = frozenset({"import_statement", "import_from_statement", "future_import_statement"})

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[^\sA-Za-z0-9_]")


class ChunkParseError(RuntimeError):
    """Raised for a file we refuse to guess at. Caller writes a ``dead_letters`` row."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


@dataclass(slots=True)
class Chunk:
    """One retrievable unit. ``chunk_id`` is content-addressed (see graph.ids)."""

    chunk_id: str
    repo: str
    path: str
    qualified_name: str | None
    enclosing_scope: str | None
    kind: str  # module | class | function | method | fragment
    start_line: int  # 1-based, addresses the REAL file (licence offset added back)
    end_line: int
    content: str
    imports_in_scope: list[str] = field(default_factory=list)
    lexical_tokens: list[str] = field(default_factory=list)
    token_estimate: int = 0
    part_index: int = 0
    part_total: int = 1
    chunking_strategy_version: str = settings.chunking_strategy_version
    parse_degraded: bool = False
    synthesized: bool = False  # class cards are assembled, not a verbatim byte range

    @property
    def is_fragment(self) -> bool:
        return self.part_total > 1

    @property
    def lexical_blob(self) -> str:
        """lexical_tokens space-joined -- the exact value of chunks.lexical_blob.

        The bm25 index reads chunks.lexical_blob (::pdb.source_code); it is the
        only field carrying the underscore-free joined form that source_code
        tokenization destroys ('_normalise_url' -> 'normaliseurl'). It cannot be a
        GENERATED column -- array_to_string() is not immutable, verified:
        'generation expression is not immutable' -- so the ingester must write it,
        and every writer must join it the SAME way. Hence one property here rather
        than a ' '.join() at each call site: leave the column empty, or join it
        differently in one writer, and joined-form matching silently stops working
        while every other query still looks fine.
        """
        return " ".join(self.lexical_tokens)


# ------------------------------------------------------------------- tokenisation


def estimate_tokens(text: str) -> int:
    """Cheap upper-ish bound on model tokens. No tokenizer dependency at ingest time."""
    return len(_TOKEN_RE.findall(text))


def split_identifier(name: str) -> list[str]:
    """``_normalise_url`` -> ``['normalise', 'url']``; ``camelCaseName`` -> 3 parts."""
    parts: list[str] = []
    for piece in name.split("_"):
        if not piece:
            continue
        parts.extend(m.group(0).lower() for m in _CAMEL_RE.finditer(piece))
    return parts


def lexical_tokens(text: str, extra: Iterable[str] = ()) -> list[str]:
    """Supplementary token array for the BM25 side of retrieval.

    Contains the split parts (matching what ``pdb.source_code`` produces, so terms
    line up) PLUS the underscore-free joined form, which the tokenizer throws away.
    Order-preserving dedupe keeps the array stable across runs, so a re-ingest with
    an unchanged file produces an unchanged row.
    """
    out: dict[str, None] = {}
    for name in list(_IDENT_RE.findall(text)) + list(extra):
        parts = split_identifier(name)
        if not parts:
            continue
        for part in parts:
            out.setdefault(part, None)
        if len(parts) > 1:
            out.setdefault("".join(parts), None)  # the form the tokenizer destroys
    return list(out)


# ------------------------------------------------------------------ tree helpers


def _definition(node: Node) -> Node:
    """Unwrap ``decorated_definition``. Decorators stay in the chunk *content*:
    ``@provide_session`` is often the entire answer to "why does this take a session?"."""
    if node.type == "decorated_definition":
        inner = node.child_by_field_name("definition")
        if inner is not None:
            return inner
    return node


def _name_of(node: Node, source: bytes) -> str | None:
    target = _definition(node)
    name = target.child_by_field_name("name")
    return node_text(name, source) if name is not None else None


def _docstring_node(definition: Node) -> Node | None:
    body = definition.child_by_field_name("body")
    if body is None or not body.named_children:
        return None
    first = body.named_children[0]
    if (
        first.type == "expression_statement"
        and first.named_children
        and first.named_children[0].type in {"string", "concatenated_string"}
    ):
        return first
    return None


def _error_byte_ratio(root: Node, total: int) -> float:
    if total == 0:
        return 0.0
    bad = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            bad += max(node.end_byte - node.start_byte, 1)
            continue  # do not double-count nested errors
        if node.has_error:
            stack.extend(node.children)
    return bad / total


def _has_error(root: Node) -> bool:
    return bool(getattr(root, "has_error", False))


# ----------------------------------------------------------------------- chunking


def chunk_file(
    path: Path | str,
    *,
    rel_path: str | None = None,
    repo: str | None = None,
    max_tokens: int = MAX_CHUNK_TOKENS,
) -> list[Chunk]:
    """Chunk a file on disk. ``rel_path`` is the scope-relative path used in ids."""
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8", errors="replace")
    return chunk_source(
        text,
        rel_path or file_path.as_posix(),
        repo=repo,
        max_tokens=max_tokens,
    )


def chunk_source(
    text: str,
    path: str,
    *,
    repo: str | None = None,
    max_tokens: int = MAX_CHUNK_TOKENS,
) -> list[Chunk]:
    """Chunk one Python source string into AST-bounded chunks.

    Raises :class:`ChunkParseError` when the file is beyond recovery; the caller is
    expected to dead-letter it and continue.
    """
    repo_name = repo or f"{settings.repo_owner}/{settings.repo_name}"

    # MUST happen before hashing: identical headers otherwise produce colliding
    # chunk_ids for short files and a dense junk neighbourhood in the index.
    body, header_lines = strip_licence_header(text)
    data = body.encode("utf-8")
    if not body.strip():
        raise ChunkParseError(path, "empty after licence-header strip")

    tree = parse_source(data)
    root = tree.root_node
    degraded = _has_error(root)
    if degraded and _error_byte_ratio(root, len(data)) > MAX_ERROR_BYTE_RATIO:
        raise ChunkParseError(path, "parse is majority ERROR nodes; not Python 3 source")

    module_imports = [
        node_text(child, data) for child in root.named_children if child.type in _IMPORT_TYPES
    ]

    chunks: list[Chunk] = []
    pending: list[Node] = []

    def flush_pending() -> None:
        if not pending:
            return
        for group in _group_statements(pending, data, max_tokens):
            content = _join_nodes(group, data)
            if not content.strip():
                continue
            chunks.append(
                _make_chunk(
                    repo=repo_name,
                    path=path,
                    qualified_name=None,
                    enclosing_scope=None,
                    kind="module",
                    content=content,
                    start_line=group[0].start_point[0] + 1 + header_lines,
                    end_line=group[-1].end_point[0] + 1 + header_lines,
                    imports=module_imports,
                    degraded=degraded,
                )
            )
        pending.clear()

    for child in root.named_children:
        if child.type == "comment":
            pending.append(child)
            continue
        if child.type in _DEF_TYPES:
            flush_pending()
            definition = _definition(child)
            if definition.type == "class_definition":
                chunks.extend(
                    _chunk_class(
                        child,
                        data,
                        repo=repo_name,
                        path=path,
                        imports=module_imports,
                        header_lines=header_lines,
                        max_tokens=max_tokens,
                        degraded=degraded,
                    )
                )
            else:
                chunks.extend(
                    _chunk_function(
                        child,
                        data,
                        repo=repo_name,
                        path=path,
                        qualified_name=_name_of(child, data) or "<anonymous>",
                        enclosing_scope=None,
                        kind="function",
                        imports=module_imports,
                        header_lines=header_lines,
                        max_tokens=max_tokens,
                        degraded=degraded,
                    )
                )
            continue
        pending.append(child)

    flush_pending()
    if not chunks:
        raise ChunkParseError(path, "produced no chunks")
    return chunks


# ------------------------------------------------------------------------ classes


def _chunk_class(
    node: Node,
    data: bytes,
    *,
    repo: str,
    path: str,
    imports: Sequence[str],
    header_lines: int,
    max_tokens: int,
    degraded: bool,
) -> list[Chunk]:
    definition = _definition(node)
    class_name = _name_of(node, data) or "<anonymous>"
    body = definition.child_by_field_name("body")
    chunks: list[Chunk] = []

    # The class card: decorators, signature, docstring, class-level attributes, and
    # one signature line per method. This is what answers "what is this class for?"
    # without dragging 2,000 lines of method bodies into one chunk.
    card_lines: list[str] = []
    if node.type == "decorated_definition":
        card_lines.extend(
            node_text(child, data) for child in node.children if child.type == "decorator"
        )
    card_lines.append(_signature_line(definition, data))

    class_imports = list(imports)
    if body is not None:
        for child in body.named_children:
            if child.type in _IMPORT_TYPES:
                class_imports.append(node_text(child, data))
            if child.type in _DEF_TYPES:
                inner = _definition(child)
                if inner.type == "function_definition":
                    card_lines.append(f"    {_signature_line(inner, data)} ...")
                else:
                    card_lines.append(f"    class {_name_of(child, data)}: ...")
                continue
            card_lines.append(_indent(node_text(child, data)))

    chunks.append(
        _make_chunk(
            repo=repo,
            path=path,
            qualified_name=class_name,
            enclosing_scope=None,
            kind="class",
            content="\n".join(card_lines),
            start_line=node.start_point[0] + 1 + header_lines,
            end_line=node.end_point[0] + 1 + header_lines,
            imports=class_imports,
            degraded=degraded,
            synthesized=True,
        )
    )

    if body is not None:
        for child in body.named_children:
            if child.type not in _DEF_TYPES:
                continue
            inner = _definition(child)
            if inner.type == "class_definition":
                chunks.extend(
                    _chunk_class(
                        child,
                        data,
                        repo=repo,
                        path=path,
                        imports=class_imports,
                        header_lines=header_lines,
                        max_tokens=max_tokens,
                        degraded=degraded,
                    )
                )
                continue
            chunks.extend(
                _chunk_function(
                    child,
                    data,
                    repo=repo,
                    path=path,
                    qualified_name=f"{class_name}.{_name_of(child, data)}",
                    enclosing_scope=class_name,
                    kind="method",
                    imports=class_imports,
                    header_lines=header_lines,
                    max_tokens=max_tokens,
                    degraded=degraded,
                )
            )
    return chunks


# ---------------------------------------------------------------------- functions


def _chunk_function(
    node: Node,
    data: bytes,
    *,
    repo: str,
    path: str,
    qualified_name: str,
    enclosing_scope: str | None,
    kind: str,
    imports: Sequence[str],
    header_lines: int,
    max_tokens: int,
    degraded: bool,
) -> list[Chunk]:
    content = node_text(node, data)
    scope_imports = list(imports) + _inner_imports(node, data)
    tokens = estimate_tokens(content)
    start = node.start_point[0] + 1 + header_lines
    end = node.end_point[0] + 1 + header_lines

    if tokens <= max_tokens:
        return [
            _make_chunk(
                repo=repo,
                path=path,
                qualified_name=qualified_name,
                enclosing_scope=enclosing_scope,
                kind=kind,
                content=content,
                start_line=start,
                end_line=end,
                imports=scope_imports,
                degraded=degraded,
            )
        ]

    return _split_oversize(
        node,
        data,
        repo=repo,
        path=path,
        qualified_name=qualified_name,
        enclosing_scope=enclosing_scope,
        imports=scope_imports,
        header_lines=header_lines,
        max_tokens=max_tokens,
        degraded=degraded,
    )


def _split_oversize(
    node: Node,
    data: bytes,
    *,
    repo: str,
    path: str,
    qualified_name: str,
    enclosing_scope: str | None,
    imports: Sequence[str],
    header_lines: int,
    max_tokens: int,
    degraded: bool,
) -> list[Chunk]:
    """Split a too-large function at STATEMENT boundaries, never mid-statement.

    Every fragment is prefixed with the decorators, signature and docstring. That
    duplication is deliberate: a fragment retrieved on its own must still say which
    function it is from, or the citation is unusable.
    """
    definition = _definition(node)
    body = definition.child_by_field_name("body")
    prelude = _prelude(node, data)
    if body is None or not body.named_children:
        return [
            _make_chunk(
                repo=repo,
                path=path,
                qualified_name=qualified_name,
                enclosing_scope=enclosing_scope,
                kind="function",
                content=node_text(node, data),
                start_line=node.start_point[0] + 1 + header_lines,
                end_line=node.end_point[0] + 1 + header_lines,
                imports=imports,
                degraded=degraded,
            )
        ]

    doc = _docstring_node(definition)
    statements = [
        child
        for child in body.named_children
        if not (doc is not None and child.id == doc.id) and child.type != "comment"
    ]
    budget = max(max_tokens - estimate_tokens(prelude), max_tokens // 4)
    groups = _group_statements(statements, data, budget)

    chunks: list[Chunk] = []
    total = len(groups) or 1
    for index, group in enumerate(groups):
        fragment = f"{prelude}\n{_join_nodes(group, data)}"
        chunks.append(
            _make_chunk(
                repo=repo,
                path=path,
                qualified_name=qualified_name,
                enclosing_scope=enclosing_scope,
                kind="fragment",
                content=fragment,
                start_line=group[0].start_point[0] + 1 + header_lines,
                end_line=group[-1].end_point[0] + 1 + header_lines,
                imports=imports,
                degraded=degraded,
                part_index=index,
                part_total=total,
                synthesized=True,
            )
        )
    return chunks


def _prelude(node: Node, data: bytes) -> str:
    """Decorators + signature + docstring, verbatim, for prepending to fragments."""
    definition = _definition(node)
    lines: list[str] = []
    if node.type == "decorated_definition":
        for child in node.children:
            if child.type == "decorator":
                lines.append(node_text(child, data))
    lines.append(_signature_line(definition, data))
    doc = _docstring_node(definition)
    if doc is not None:
        lines.append(_indent(node_text(doc, data)))
    return "\n".join(lines)


def _signature_line(definition: Node, data: bytes) -> str:
    body = definition.child_by_field_name("body")
    end = body.start_byte if body is not None else definition.end_byte
    text = data[definition.start_byte : end].decode("utf-8", errors="replace").rstrip()
    return text if text.endswith(":") else f"{text}:"


def _inner_imports(node: Node, data: bytes) -> list[str]:
    """Function-local imports. Airflow uses them constantly to break import cycles,
    and the cycle is frequently the rationale being asked about."""
    out: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in _IMPORT_TYPES:
            out.append(node_text(current, data))
            continue
        stack.extend(current.named_children)
    return out


# ------------------------------------------------------------------------ grouping


def _group_statements(nodes: Sequence[Node], data: bytes, max_tokens: int) -> list[list[Node]]:
    """Greedily pack sibling statements up to the budget; a single oversize
    statement becomes its own group rather than being cut."""
    groups: list[list[Node]] = []
    current: list[Node] = []
    size = 0
    for node in nodes:
        cost = estimate_tokens(node_text(node, data))
        if current and size + cost > max_tokens:
            groups.append(current)
            current, size = [], 0
        current.append(node)
        size += cost
    if current:
        groups.append(current)
    return groups


def _join_nodes(nodes: Sequence[Node], data: bytes) -> str:
    """Reproduce the original byte span so indentation and blank lines survive."""
    if not nodes:
        return ""
    start = nodes[0].start_byte
    end = nodes[-1].end_byte
    # Back up to the start of the first node's line to keep its indentation.
    line_start = data.rfind(b"\n", 0, start) + 1
    return data[line_start:end].decode("utf-8", errors="replace").rstrip()


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line if line.strip() else line for line in text.split("\n"))


# -------------------------------------------------------------------------- build


def _make_chunk(
    *,
    repo: str,
    path: str,
    qualified_name: str | None,
    enclosing_scope: str | None,
    kind: str,
    content: str,
    start_line: int,
    end_line: int,
    imports: Sequence[str],
    degraded: bool,
    part_index: int = 0,
    part_total: int = 1,
    synthesized: bool = False,
) -> Chunk:
    # Fragments of the same function share a qualified_name, so the part index has
    # to reach the id or every fragment after the first would collapse onto it.
    id_name = qualified_name if part_total == 1 else f"{qualified_name}#{part_index}"
    extra = [qualified_name] if qualified_name else []
    if enclosing_scope:
        extra.append(enclosing_scope)
    return Chunk(
        chunk_id=chunk_id(repo, path, id_name, content),
        repo=repo,
        path=path,
        qualified_name=qualified_name,
        enclosing_scope=enclosing_scope,
        kind=kind,
        start_line=start_line,
        end_line=end_line,
        content=content,
        imports_in_scope=list(dict.fromkeys(imports)),
        lexical_tokens=lexical_tokens(content, extra),
        token_estimate=estimate_tokens(content),
        part_index=part_index,
        part_total=part_total,
        parse_degraded=degraded,
        synthesized=synthesized,
    )


def iter_chunks(
    paths: Iterable[tuple[str, str]],
    *,
    repo: str | None = None,
    max_tokens: int = MAX_CHUNK_TOKENS,
) -> Iterator[tuple[str, list[Chunk] | ChunkParseError]]:
    """Chunk many ``(rel_path, text)`` pairs with per-document isolation.

    Yields either the chunks or the error, never raises: one unparseable file in a
    605-file corpus must cost one dead-letter row, not the whole run.
    """
    for rel_path, text in paths:
        try:
            yield rel_path, chunk_source(text, rel_path, repo=repo, max_tokens=max_tokens)
        except ChunkParseError as exc:
            yield rel_path, exc
        except Exception as exc:  # noqa: BLE001 - unexpected parser failure is still isolated
            yield rel_path, ChunkParseError(rel_path, f"{type(exc).__name__}: {exc}")
