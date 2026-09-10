"""The hash ladder for function identity: h0 (exact) -> h3 (alpha-renamed shape).

A function moves file, gets reformatted, gets its locals renamed, gets annotated —
and it is still the same function, so the commit that explains it is still the
right citation. One hash cannot express that. A *ladder* can: compute all four,
compare strictest first, and let match confidence cascade.

    h0  exact text            byte-identical after newline/NFC normalisation
    h1  token stream          comments and layout dropped
    h2  AST shape             structure + identifier names, literals kept
    h3  alpha-renamed AST     locals -> v0, v1, ...; constants -> type; no annotations

The rung that pays for itself immediately is **h1**: the Black reformat commit
touched 1,066 files and changed no tokens, so every one of those functions keeps
its h1 and the identity chain survives the reformat for free. Magic trailing
commas do perturb the token stream — those fall through to h2/h3, which are
structural and do not see a trailing comma at all.

Why the tree-sitter s-expression and NOT ``ast.dump``
-----------------------------------------------------
``ast.dump`` is not stable across Python minor versions: 3.13+ omits fields left
at their default while 3.12 emits them all, so the *same source* hashes
differently depending on which interpreter ran the ingest. That turns into
"identity broke" bugs that are really "CI upgraded Python" bugs. tree-sitter's
s-expression is pinned by the grammar package version, which we record verbatim
in :func:`ast_norm_version` along with the interpreter, so any hash can be
attributed to the exact toolchain that produced it.
"""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Literal

from provenance.graph.ids import normalize_content
from provenance.parse import (
    grammar_version,
    node_text,
    parse_source,
    tree_sitter_version,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

__all__ = [
    "NORM_ALGO_VERSION",
    "HashLadder",
    "MatchConfidence",
    "ast_norm_version",
    "hash_ladder",
    "token_stream",
    "shape_sexp",
    "alpha_sexp",
    "match_confidence",
]

# Bump when any rung's *algorithm* changes. It is recorded in ast_norm_version, and
# it is a COLUMN on the row, never an input to chunk_id (see provenance.graph.ids).
NORM_ALGO_VERSION = "ladder-v1"

MatchConfidence = Literal["exact", "token", "shape", "alpha", "none"]

# Literal node types collapsed to their type name at h3.
_CONST_TYPES: dict[str, str] = {
    "string": "STR",
    "concatenated_string": "STR",
    "integer": "INT",
    "float": "FLOAT",
    "true": "BOOL",
    "false": "BOOL",
    "none": "NONE",
    "ellipsis": "ELLIPSIS",
}

# Anonymous tokens that carry no meaning the node type does not already carry.
# Operators are NOT in here: `a + b` and `a - b` are both `binary_operator`, so
# dropping the operator text would collapse two different functions onto one hash.
_STRUCTURAL_TOKENS = frozenset("()[]{},:;.") | {"def", "class", "lambda", "->", "..."}

_STRING_RE = re.compile(r"^(?P<prefix>[A-Za-z]*)(?P<quote>'''|\"\"\"|'|\")(?P<body>.*)$", re.DOTALL)

_ALWAYS_KEEP = frozenset({"self", "cls"})

_BINDING_HOLDERS = frozenset(
    {
        "assignment",
        "augmented_assignment",
        "for_statement",
        "for_in_clause",
        "named_expression",
    }
)


@dataclass(frozen=True, slots=True)
class HashLadder:
    h0: str
    h1: str
    h2: str
    h3: str
    ast_norm_version: str

    def as_row(self) -> dict[str, str]:
        """Column names match the ``function_identity`` table."""
        return {
            "h0_exact": self.h0,
            "h1_tokens": self.h1,
            "h2_shape": self.h2,
            "h3_alpha": self.h3,
            "ast_norm_version": self.ast_norm_version,
        }


@lru_cache(maxsize=1)
def ast_norm_version() -> str:
    """Exact grammar + runtime + interpreter that produced a hash.

    The interpreter is recorded even though the s-expression does not depend on it:
    if a rung ever *does* drift, this is the field that tells you which environment
    to blame instead of guessing.
    """
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return (
        f"{NORM_ALGO_VERSION}"
        f"|grammar=tree-sitter-python@{grammar_version()}"
        f"|runtime=tree-sitter@{tree_sitter_version()}"
        f"|python={py}"
    )


def _digest(scheme: str, payload: str) -> str:
    # sha256, never builtin hash(): PYTHONHASHSEED salts hash() per process, so a
    # value stored today would not compare equal to the same value tomorrow.
    return hashlib.sha256(f"{scheme}\x00{payload}".encode()).hexdigest()


# --------------------------------------------------------------------------- h1


def _canonical_string(text: str) -> str:
    """Collapse quote style and prefix casing; Black rewrites `'x'` to `"x"`."""
    m = _STRING_RE.match(text)
    if not m:
        return f"STR:{text}"
    prefix = m.group("prefix").lower().replace("u", "")
    quote = m.group("quote")
    body = m.group("body")
    if body.endswith(quote):
        body = body[: -len(quote)]
    return f"STR:{prefix}:{body}"


def token_stream(node: Node, source: bytes) -> list[str]:
    """Leaf tokens with comments dropped and strings canonicalised.

    Layout disappears because tree-sitter leaves carry no whitespace, which is
    exactly what makes h1 immune to a reformat.
    """
    tokens: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "comment":
            continue
        if current.type in {"string", "concatenated_string"}:
            tokens.append(_canonical_string(node_text(current, source)))
            continue
        if current.child_count == 0:
            text = node_text(current, source)
            if text:
                tokens.append(text)
            continue
        stack.extend(reversed(current.children))
    return tokens


# ----------------------------------------------------------------------- h2 / h3


def _skip_annotation_ids(node: Node) -> set[int]:
    """Node ids of annotation subtrees, which h3 drops entirely."""
    skip: set[int] = set()
    for field in ("return_type", "type"):
        child = node.child_by_field_name(field)
        if child is not None:
            skip.add(child.id)
    return skip


def _sexp(
    node: Node,
    source: bytes,
    *,
    rename: dict[str, str] | None,
    drop_annotations: bool,
    collapse_consts: bool,
    verbatim: frozenset[int] = frozenset(),
) -> str:
    if node.type == "comment":
        return ""
    if collapse_consts and node.type in _CONST_TYPES:
        return _CONST_TYPES[node.type]

    if node.child_count == 0:
        text = node_text(node, source)
        if node.type == "identifier":
            if rename is not None and node.id not in verbatim:
                return rename.get(text, text)
            return text
        if not node.is_named:
            return "" if text in _STRUCTURAL_TOKENS else text
        return f"{node.type}:{text}" if text else node.type

    skip = _skip_annotation_ids(node) if drop_annotations else frozenset()
    keep_verbatim = set(verbatim)
    # An attribute name and a keyword-argument name are API surface, not locals:
    # `x.timeout` and `f(timeout=1)` must survive alpha-renaming intact.
    if node.type == "attribute":
        attr = node.child_by_field_name("attribute")
        if attr is not None:
            keep_verbatim.add(attr.id)
    if node.type == "keyword_argument":
        name = node.child_by_field_name("name")
        if name is not None:
            keep_verbatim.add(name.id)

    parts: list[str] = []
    for child in node.children:
        if child.id in skip:
            continue
        rendered = _sexp(
            child,
            source,
            rename=rename,
            drop_annotations=drop_annotations,
            collapse_consts=collapse_consts,
            verbatim=frozenset(keep_verbatim),
        )
        if rendered:
            parts.append(rendered)
    inner = " ".join(parts)
    return f"({node.type} {inner})" if inner else f"({node.type})"


def shape_sexp(node: Node, source: bytes) -> str:
    """h2: full structure with identifiers and literals kept."""
    return _sexp(node, source, rename=None, drop_annotations=False, collapse_consts=False)


def alpha_sexp(node: Node, source: bytes) -> str:
    """h3: locals renamed in first-appearance order, constants typed, annotations dropped."""
    rename = _rename_map(node, source)
    return _sexp(node, source, rename=rename, drop_annotations=True, collapse_consts=True)


# ------------------------------------------------------------------ alpha renaming


def _first_identifier(node: Node) -> Node | None:
    named = node.child_by_field_name("name")
    if named is not None and named.type == "identifier":
        return named
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "identifier":
            return current
        stack.extend(reversed(current.named_children))
    return None


def _identifiers_in(node: Node) -> list[Node]:
    """Identifiers bound by a target expression. `self.x = 1` binds nothing."""
    out: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in {"attribute", "subscript"}:
            continue  # assigns *through* a name, does not bind it
        if current.type == "identifier":
            out.append(current)
            continue
        stack.extend(reversed(current.named_children))
    return out


def _rename_map(root: Node, source: bytes) -> dict[str, str]:
    """Locals -> v0, v1, ... in first-appearance (byte offset) order.

    ``self`` and ``cls`` keep their names: they are the receiver, and renaming them
    would make an instance method indistinguishable from a free function that
    happens to take one argument.
    """
    bindings: list[tuple[int, str]] = []
    keep: set[str] = set(_ALWAYS_KEEP)

    stack = [root]
    while stack:
        node = stack.pop()
        kind = node.type

        if kind in {"global_statement", "nonlocal_statement"}:
            # Declared non-local: renaming it would break the link to module scope.
            for ident in node.named_children:
                if ident.type == "identifier":
                    keep.add(node_text(ident, source))

        elif kind == "parameters" or kind == "lambda_parameters":
            for child in node.named_children:
                ident = _first_identifier(child) if child.type != "identifier" else child
                if ident is not None:
                    bindings.append((ident.start_byte, node_text(ident, source)))

        elif kind in _BINDING_HOLDERS:
            target = node.child_by_field_name("left") or node.child_by_field_name("name")
            if target is not None:
                for ident in _identifiers_in(target):
                    bindings.append((ident.start_byte, node_text(ident, source)))

        elif kind == "as_pattern":
            alias = node.child_by_field_name("alias") or (
                node.named_children[-1] if node.named_children else None
            )
            if alias is not None:
                for ident in _identifiers_in(alias):
                    bindings.append((ident.start_byte, node_text(ident, source)))

        elif kind in {"function_definition", "class_definition"} and node.id != root.id:
            name = node.child_by_field_name("name")
            if name is not None:
                bindings.append((name.start_byte, node_text(name, source)))

        stack.extend(node.named_children)

    mapping: dict[str, str] = {}
    for _offset, name in sorted(bindings, key=lambda pair: pair[0]):
        if name in keep or name in mapping or name.startswith("__"):
            continue
        mapping[name] = f"v{len(mapping)}"
    return mapping


# --------------------------------------------------------------------- the ladder


def _target_node(source: str) -> tuple[Node, bytes]:
    """Parse and return the node the ladder applies to.

    If the snippet is exactly one definition (the normal case: a chunk produced by
    the chunker) we hash that definition, so a function's hashes do not change when
    the module around it changes.
    """
    data = source.encode("utf-8")
    tree = parse_source(data)
    root = tree.root_node
    named = [child for child in root.named_children if child.type != "comment"]
    if len(named) == 1 and named[0].type in {
        "function_definition",
        "class_definition",
        "decorated_definition",
    }:
        return named[0], data
    return root, data


def hash_ladder(source: str) -> HashLadder:
    """Compute all four rungs for one function/class/chunk body."""
    canonical = normalize_content(source)
    node, data = _target_node(canonical)
    return HashLadder(
        h0=_digest("h0", canonical),
        h1=_digest("h1", "\x00".join(token_stream(node, data))),
        h2=_digest("h2", shape_sexp(node, data)),
        h3=_digest("h3", alpha_sexp(node, data)),
        ast_norm_version=ast_norm_version(),
    )


def match_confidence(left: HashLadder, right: HashLadder) -> MatchConfidence:
    """Strictest-first cascade. The label is what the UI shows next to a citation."""
    if left.h0 == right.h0:
        return "exact"
    if left.h1 == right.h1:
        return "token"
    if left.h2 == right.h2:
        return "shape"
    if left.h3 == right.h3:
        return "alpha"
    return "none"
