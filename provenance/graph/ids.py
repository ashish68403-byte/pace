"""Stable, content-addressed identity for chunks.

What this module is for: producing a chunk_id that is identical on every
machine, in every process, in every run, for the same piece of source.

What failure it prevents: a golden set that dies on the first chunker change.
The eval anchors gold evidence on commit_sha / pr_number / issue_number /
review_comment_id / (path, qualified_name) and resolves those to chunk_ids at
scoring time -- but that resolution is only worth anything if a chunk_id is a
deterministic function of content. Two rules make it so, and both are load
bearing:

  1. NEVER use Python's builtin hash(). str.__hash__ is salted per process
     (PYTHONHASHSEED), so it returns a different value for the same string in
     the next interpreter. A "stable id" built on it is stable only until the
     process restarts.

  2. Normalize before hashing. The same function read through a Windows
     checkout, an editor that strips trailing whitespace, or a file with a
     BOM-ish composed/decomposed unicode difference must hash the same, or the
     chunk churns for reasons that have nothing to do with the code changing.
"""

from __future__ import annotations

import hashlib
import unicodedata

# Bump ONLY when normalize_content() or the payload layout changes -- i.e.
# when the same source is deliberately meant to hash differently. It is part
# of the payload so old and new ids cannot collide.
CHUNK_ID_SCHEME = "v1"


def normalize_content(text: str) -> str:
    """Canonicalize source text before hashing.

    NFC folds composed/decomposed unicode to one spelling; CRLF and lone CR
    become LF; trailing whitespace per line and leading/trailing blank lines
    are dropped. Everything else -- indentation, blank lines inside the body,
    the code itself -- is preserved, because those are real differences.
    """
    t = unicodedata.normalize("NFC", text)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    # `l` is kept from the frozen contract source; noqa because ruff's E741
    # would otherwise reject a line this module is not allowed to reword.
    return "\n".join(l.rstrip() for l in t.split("\n")).strip("\n")  # noqa: E741


def chunk_id(repo: str, path: str, qualified_name: str | None, content: str) -> str:
    """Return the content-addressed id for a chunk.

    The payload is joined on NUL because NUL cannot occur in a path, a
    qualified name, or (after normalization) in source text -- so no two
    distinct field tuples can serialize to the same byte string. Joining on
    ':' or '/' would let ("a/b", "c") and ("a", "b/c") collide.

    Deliberately NOT in the hash: chunking_strategy_version,
    embedding_model_version, prompt_version. Those are provenance of
    DERIVATION, and they live as columns on the row. Folding them in would
    change every chunk_id the first time the chunker is tuned, which would
    invalidate every cached embedding and -- fatally -- every golden-set
    anchor, turning a one-line parser fix into a full re-annotation of the
    eval. The whole point of keying gold evidence on commits and
    (path, qualified_name) is that the id layer must be allowed to move
    underneath it.
    """
    payload = "\x00".join(
        [CHUNK_ID_SCHEME, repo, path, qualified_name or "", normalize_content(content)]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
