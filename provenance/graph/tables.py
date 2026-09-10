"""Canonical physical names for everything the query layer touches.

Every table and column name in this project appears here exactly once. Six
subtrees were written in parallel against a schema they could not see, and each
invented its own spelling: `chunk` vs `chunks`, `repo` vs `repo_id`, `lang` vs
`language`, `tombstoned_at` vs `tombstoned_at_commit`. Every one of those is a
runtime `relation does not exist` or `column does not exist`, discoverable only
by executing the query -- so a name that drifts costs a debugging session, not a
type error.

Import the constant. Do not type the string.
"""

from __future__ import annotations

from typing import Final

# --- core -----------------------------------------------------------------
REPOSITORIES: Final = "repositories"
CHUNKS: Final = "chunks"
COMMITS: Final = "commits"
FILES: Final = "files"

# chunks columns that differ from the obvious spelling
COL_REPO_ID: Final = "repo_id"  # bigint FK, NOT the "owner/name" string
COL_LANGUAGE: Final = "language"  # not `lang`
COL_TOMBSTONE: Final = "tombstoned_at_commit"  # not `tombstoned_at`
COL_CHUNK_ID: Final = "chunk_id"  # sha256 hex, the real identity
COL_CHUNK_ROW_ID: Final = "chunk_row_id"  # bigint surrogate, bm25 key_field ONLY

# --- bm25 -----------------------------------------------------------------
# The single ParadeDB index on `chunks`. pg_search allows exactly one such
# index per relation, forbids the same column under two casts, and returns hits
# keyed on key_field -- so paradedb.score() must be passed CHUNK_ROW_ID, and
# retrieval must join back to chunk_id.
BM25_INDEX: Final = "chunks_bm25_idx"
BM25_KEY_FIELD: Final = COL_CHUNK_ROW_ID
BM25_FIELD_CONTENT: Final = "content"  # pdb.source_code -> split tokens
BM25_FIELD_QUALNAME: Final = "qualified_name"  # pdb.literal     -> exact symbol
BM25_FIELD_LEXICAL: Final = "lexical_blob"  # pdb.source_code -> joined forms

# --- vectors --------------------------------------------------------------
# Physical tables are blue/green; readers resolve the alias, never hardcode.
INDEX_ALIASES: Final = "index_aliases"
ALIAS_CODE: Final = "code_current"
ALIAS_PROSE: Final = "prose_current"
EMBED_CODE_TABLES: Final = ("embeddings_code_blue", "embeddings_code_green")
EMBED_PROSE_TABLES: Final = ("embeddings_prose_blue", "embeddings_prose_green")

# HNSW is built on a halfvec cast. A probe that is not cast identically does not
# use the index -- it silently falls back to a sequential scan that still
# returns correct rows, so this is a performance bug that no test catches.
VECTOR_CAST: Final = "halfvec"


def halfvec_probe(dim: int) -> str:
    """Return the ORDER BY fragment that actually hits the HNSW index."""
    return f"embedding::halfvec({dim}) <=> %s::halfvec({dim})"


# --- evidence -------------------------------------------------------------
CHUNK_EVIDENCE: Final = "chunk_evidence"
