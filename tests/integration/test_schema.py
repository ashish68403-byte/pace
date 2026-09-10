"""What `alembic upgrade head` actually builds, asserted against a live server.

Marked ``db``: needs the pinned ParadeDB container (PostgreSQL 18.6 + pgvector
0.8.4 + pg_search 0.25.7). `tests/conftest.py` skips the whole module cleanly
when that container is not reachable, so the default gate still runs anywhere.

These are not "does the table exist" smoke tests. Each one pins a property that
was got wrong somewhere in this tree and whose failure mode is a *correct-looking
wrong answer*:

* the physical names -- `chunks` not `chunk`, `language` not `lang`, `repo_id`
  bigint not the `owner/name` slug, `tombstoned_at_commit` not `tombstoned_at`.
  The whole retrieval layer was written against the other spellings and every one
  of them is a runtime `relation does not exist`, discoverable only by executing
  the query (audit #2, #3, #4).
* exactly ONE ParadeDB index on `chunks`, keyed on `chunk_row_id`. pg_search
  permits one per relation and rejects a column indexed twice under two casts;
  both limits were hit for real. `paradedb.score()` must be passed the key field.
* the PARTIAL HNSW index. pgvector's HNSW is a POST-filter, so a plain index plus
  `WHERE visibility_group = 'public'` searches vectors the caller may not see and
  then discards them -- fewer than k rows returned, a healthy-looking index scan
  in EXPLAIN, and a timing side channel. Only a partial index is a real
  pre-filter.
* the GiST exclusion constraint on `blame_ranges`, which is the only thing
  stopping two blame ranges from claiming the same line of the same snapshot.

Everything is read out of the catalogue rather than from the migration text: a
test that re-reads the DDL it is testing proves nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from provenance.graph.tables import (
    BM25_FIELD_CONTENT,
    BM25_FIELD_LEXICAL,
    BM25_FIELD_QUALNAME,
    BM25_INDEX,
    BM25_KEY_FIELD,
    CHUNK_EVIDENCE,
    CHUNKS,
    COL_CHUNK_ID,
    COL_CHUNK_ROW_ID,
    COL_LANGUAGE,
    COL_REPO_ID,
    COL_TOMBSTONE,
    EMBED_CODE_TABLES,
    EMBED_PROSE_TABLES,
    INDEX_ALIASES,
    REPOSITORIES,
)

pytestmark = pytest.mark.db

HEAD_REVISION = "0005_chunk_evidence"

CORE_TABLES = (
    REPOSITORIES,
    "ingest_runs",
    "dead_letters",
    "commits",
    "commit_parents",
    "files",
    "file_revisions",
    CHUNKS,
)
LINEAGE_TABLES = ("symbol_lineage", "symbol_versions", "lineage_edges", "blame_ranges")
GITHUB_TABLES = (
    "pull_requests",
    "issues",
    "issue_comments",
    "review_comments",
    "edge_commit_pr",
    "edge_pr_issue",
    "edge_symbol_commit",
)
VECTOR_TABLES = ("embedding_models", INDEX_ALIASES, *EMBED_CODE_TABLES, *EMBED_PROSE_TABLES)


def _scalar(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def _column_types(conn: Any, table: str) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table,),
    ).fetchall()
    return {name: dtype for name, dtype in rows}


# --------------------------------------------------------------------------- tables


def test_migration_head_is_the_last_revision(migrated_database: Any) -> None:
    assert _scalar(migrated_database, "SELECT version_num FROM alembic_version") == HEAD_REVISION


@pytest.mark.parametrize(
    "table", [*CORE_TABLES, *LINEAGE_TABLES, *GITHUB_TABLES, *VECTOR_TABLES, CHUNK_EVIDENCE]
)
def test_every_table_exists(migrated_database: Any, table: str) -> None:
    assert _scalar(migrated_database, "SELECT to_regclass(%s)", (f"public.{table}",)) is not None


def test_chunk_evidence_carries_the_scoring_time_lookup_index(migrated_database: Any) -> None:
    """Migration 0005. Without this table the golden set cannot survive a re-chunk:
    it is the only path from a gold commit sha / PR number to a current chunk id."""
    columns = _column_types(migrated_database, CHUNK_EVIDENCE)
    assert columns[COL_REPO_ID] == "bigint"
    assert columns["evidence_kind"] == "USER-DEFINED"  # the artifact_kind enum
    assert columns["evidence_key"] == "text"
    assert columns["source"] == "text"

    definition = _scalar(
        migrated_database,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'chunk_evidence_lookup_idx'",
    )
    assert definition is not None
    # The order matters: (repo_id, evidence_kind, evidence_key) is the prefix the
    # resolver's equality predicates use. A different order still answers the
    # query, by scanning.
    assert "(repo_id, evidence_kind, evidence_key)" in definition


# ---------------------------------------------------------------- physical names


def test_chunks_uses_the_spellings_the_query_layer_imports(migrated_database: Any) -> None:
    columns = _column_types(migrated_database, CHUNKS)

    assert columns[COL_CHUNK_ID] == "text"  # sha256_hex is a domain over text
    assert columns[COL_CHUNK_ROW_ID] == "bigint"
    assert columns[COL_REPO_ID] == "bigint"
    assert columns[COL_LANGUAGE] == "text"
    assert COL_TOMBSTONE in columns
    assert "lexical_blob" in columns

    # The spellings six subtrees invented, none of which exist. Asserting their
    # ABSENCE is what stops someone "fixing" tables.py to match a query.
    for wrong in ("lang", "repo", "tombstoned_at", "embedding"):
        assert wrong not in columns, f"chunks.{wrong} exists; tables.py and the audit disagree"


def test_repo_id_is_a_bigint_foreign_key_not_a_slug(migrated_database: Any) -> None:
    """`WHERE repo_id = 'apache/airflow'` is `invalid input syntax for type bigint`
    -- and only at execution time, after a fixture load and a scoring loop."""
    import psycopg

    assert _column_types(migrated_database, REPOSITORIES)[COL_REPO_ID] == "bigint"
    with pytest.raises(psycopg.DataError):
        migrated_database.execute(
            f"SELECT 1 FROM {CHUNKS} WHERE {COL_REPO_ID} = %s", ("apache/airflow",)
        ).fetchone()


# ----------------------------------------------------------------------- bm25


def _paradedb_indexes(conn: Any, table: str | None = None) -> list[tuple[str, str]]:
    """[(table, index)] for every ParadeDB (bm25) index in the database."""
    sql = """
        SELECT t.relname, i.relname
        FROM pg_index x
        JOIN pg_class i ON i.oid = x.indexrelid
        JOIN pg_class t ON t.oid = x.indrelid
        JOIN pg_am   a ON a.oid = i.relam
        WHERE a.amname = 'bm25'
    """
    params: tuple[Any, ...] = ()
    if table is not None:
        sql += " AND t.relname = %s"
        params = (table,)
    return [(t, i) for t, i in conn.execute(sql, params).fetchall()]


def test_chunks_has_exactly_one_paradedb_index(migrated_database: Any) -> None:
    """MEASURED: "a relation may only have one ParadeDB index". So the
    one-index-per-tokenizer design is not available, and a second one added later
    fails at migration time rather than at query time."""
    found = _paradedb_indexes(migrated_database, CHUNKS)
    assert found == [(CHUNKS, BM25_INDEX)]


def test_no_relation_anywhere_has_two_paradedb_indexes(migrated_database: Any) -> None:
    by_table: dict[str, list[str]] = {}
    for table, index in _paradedb_indexes(migrated_database):
        by_table.setdefault(table, []).append(index)
    assert all(len(v) == 1 for v in by_table.values()), by_table


def test_bm25_index_shape_matches_what_retrieval_queries(migrated_database: Any) -> None:
    """Three DISTINCT columns, each with the tokenizer that suits it, and
    `key_field = chunk_row_id` -- so `paradedb.score()` takes the surrogate and
    retrieval must join back to chunk_id."""
    definition = _scalar(
        migrated_database,
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = %s",
        (BM25_INDEX,),
    )
    assert definition is not None

    # pg_get_indexdef renders casts as `((content)::pdb.source_code)` and
    # reloptions as `key_field='chunk_row_id'`; parentheses, spacing and quoting
    # are formatting, not contract, so they are normalised away here.
    flat = definition.lower().replace(" ", "").replace("(", "").replace(")", "").replace("'", "")

    assert f"key_field={BM25_KEY_FIELD}" in flat
    assert f"{BM25_FIELD_CONTENT}::pdb.source_code" in flat
    assert f"{BM25_FIELD_QUALNAME}::pdb.literal" in flat
    assert f"{BM25_FIELD_LEXICAL}::pdb.source_code" in flat

    # MEASURED: "indexed attribute content defined more than once". The same
    # column under two casts is rejected outright, so `content` is cast exactly
    # once, as a source_code field, and never as a literal.
    assert flat.count(f"{BM25_FIELD_CONTENT}::") == 1
    assert f"{BM25_FIELD_CONTENT}::pdb.literal" not in flat


# ----------------------------------------------------------------------- hnsw


def _index_row(conn: Any, name: str) -> tuple[str, bool, str] | None:
    row = conn.execute(
        """
        SELECT a.amname, x.indpred IS NOT NULL AS partial, pg_get_indexdef(x.indexrelid)
        FROM pg_index x
        JOIN pg_class i ON i.oid = x.indexrelid
        JOIN pg_am   a ON a.oid = i.relam
        WHERE i.relname = %s
        """,
        (name,),
    ).fetchone()
    return None if row is None else (row[0], bool(row[1]), row[2])


def test_partial_hnsw_index_is_a_real_pre_filter(migrated_database: Any) -> None:
    row = _index_row(migrated_database, "embeddings_code_blue_hnsw_public")
    assert row is not None, "the visibility pre-filter index is missing"
    amname, partial, definition = row

    assert amname == "hnsw"
    assert partial is True, (
        "index is not partial: a non-partial HNSW plus a WHERE clause is a "
        "POST-filter -- it returns fewer than k rows and walks vectors the "
        "caller is not permitted to see"
    )
    assert "visibility_group" in definition


def test_hnsw_indexes_are_built_on_the_halfvec_cast(migrated_database: Any) -> None:
    """`provenance.graph.tables.halfvec_probe()` must cast IDENTICALLY. A probe
    cast differently still returns correct rows -- by sequential scan. No test
    downstream catches that, which is why it is caught here."""
    for name in ("embeddings_code_blue_hnsw", "embeddings_code_blue_hnsw_public"):
        row = _index_row(migrated_database, name)
        assert row is not None, f"{name} is missing"
        amname, _, definition = row
        assert amname == "hnsw"
        assert "halfvec(384)" in definition.replace(" ", "")
        assert "halfvec_cosine_ops" in definition

    plain = _index_row(migrated_database, "embeddings_code_blue_hnsw")
    assert plain is not None
    assert plain[1] is False  # the contrast that makes the partial index meaningful


# ------------------------------------------------------------------- exclusion


def test_blame_ranges_has_a_gist_exclusion_constraint(migrated_database: Any) -> None:
    """Two blame ranges cannot claim the same line of the same file snapshot.

    `btree_gist` (created in 0001) is what supplies `=` for bigint and text
    alongside `&&` for int4range; without the extension this constraint cannot be
    created at all.
    """
    row = migrated_database.execute(
        """
        SELECT c.contype, a.amname
        FROM pg_constraint c
        JOIN pg_class i ON i.oid = c.conindid
        JOIN pg_am   a ON a.oid = i.relam
        WHERE c.conname = 'blame_ranges_no_overlap'
        """
    ).fetchone()
    assert row is not None, "blame_ranges_no_overlap is missing"
    contype, amname = row
    assert contype == "x"  # exclusion
    assert amname == "gist"


def test_required_extensions_are_installed(migrated_database: Any) -> None:
    from provenance.graph.db import REQUIRED_EXTENSIONS

    installed = {
        name for (name,) in migrated_database.execute("SELECT extname FROM pg_extension").fetchall()
    }
    assert set(REQUIRED_EXTENSIONS) <= installed
