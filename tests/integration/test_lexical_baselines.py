"""Slices B5a and B5b against pace-db: the lexical retrievers on history_repo's pool."""

from __future__ import annotations

import pytest

from provenance.eval.leakage import PoolIndex, load_pool_from_db
from provenance.eval.lexical_baselines import Bm25IdentRetriever, GrepRetriever
from provenance.graph.tables import RATIONALE_KINDS
from provenance.retrieve.lexical import Bm25Retriever, lexical_search

pytestmark = pytest.mark.db

QUESTION = "Why did `alpha` change in January 2020?"
PLANTED = "f" * 64


def plant_code_chunk(conn, repo_id, sha):
    """A chunk of a kind outside the pool, full of the query's word."""
    conn.execute(
        "INSERT INTO chunks (chunk_id, repo_id, path, kind, language, start_line, end_line, "
        "content, content_sha, chunking_strategy_version, first_seen_commit, last_seen_commit) "
        "VALUES (%s, %s, 'core/src/pkg/alpha.py', 'chunk', 'python', 1, 2, %s, %s, 'test', %s, %s)",
        (PLANTED, repo_id, "def alpha():\n    return alpha(alpha)", "e" * 64, sha, sha),
    )


def pool_ids(query, repo_id):
    rows = query(
        "SELECT chunk_id FROM chunks WHERE repo_id = %s AND kind = ANY(%s::artifact_kind[])",
        repo_id,
        list(RATIONALE_KINDS),
    )
    return {cid for (cid,) in rows}


def test_the_kinds_filter_shuts_out_a_planted_code_chunk(pooled, test_repo_id, migrated_database):
    plant_code_chunk(migrated_database, test_repo_id, pooled.sha["c1"])
    everything = {h.chunk_id for h in lexical_search("alpha", limit=50, repo_id=test_repo_id)}
    pool_only = {
        h.chunk_id
        for h in lexical_search("alpha", limit=50, repo_id=test_repo_id, kinds=RATIONALE_KINDS)
    }
    assert PLANTED in everything  # without the filter the code chunk is found
    assert pool_only and PLANTED not in pool_only  # with it, only the pool


def test_each_retriever_returns_pool_documents_only(pooled, test_repo_id, migrated_database, query):
    plant_code_chunk(migrated_database, test_repo_id, pooled.sha["c1"])
    index = PoolIndex(load_pool_from_db(migrated_database, test_repo_id))
    pool = pool_ids(query, test_repo_id)
    retrievers = (GrepRetriever(index), Bm25IdentRetriever(index), Bm25Retriever(test_repo_id))
    for retriever in retrievers:
        found = list(retriever.retrieve(QUESTION, None, 10))
        assert found and set(found) <= pool, retriever.name


def test_loose_identifiers_find_a_document_that_names_only_some_parts(pooled, test_repo_id, query):
    (move,) = [
        cid
        for (cid,) in query(
            "SELECT chunk_id FROM chunks "
            "WHERE repo_id = %s AND kind = 'commit' AND content LIKE %s",
            test_repo_id,
            "move pkg into core%",
        )
    ]
    question = "Why did `move_pkg_core` change?"

    def found(identifiers):
        hits = lexical_search(
            question, limit=10, repo_id=test_repo_id, kinds=RATIONALE_KINDS, identifiers=identifiers
        )
        return [hit.chunk_id for hit in hits]

    assert found("loose")[0] == move
    assert move not in found("phrase")  # no "move pkg core" phrase in "move pkg into core/src"
