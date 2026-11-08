"""Slice B5b against pace-db: dense retrieval over the pool only, and the fused row."""

from __future__ import annotations

import pytest
from vector_fakes import FakeEmbedder, embed_pool_rows, write_vectors

from provenance.retrieve.dense import DenseRetriever, exact_search
from provenance.retrieve.fusion import RrfRetriever
from provenance.retrieve.lexical import Bm25Retriever

pytestmark = pytest.mark.db

QUESTION = "Why did `alpha` change in January 2020?"
PLANTED = "f" * 64


def plant_code_chunk(conn, repo_id, sha):
    """A chunk of a kind outside the pool."""
    conn.execute(
        "INSERT INTO chunks (chunk_id, repo_id, path, kind, language, start_line, end_line, "
        "content, content_sha, chunking_strategy_version, first_seen_commit, last_seen_commit) "
        "VALUES (%s, %s, 'core/src/pkg/alpha.py', 'chunk', 'python', 1, 2, %s, %s, 'test', %s, %s)",
        (PLANTED, repo_id, "def alpha():\n    return alpha(alpha)", "e" * 64, sha, sha),
    )


def test_dense_searches_the_pool_only_and_needs_a_vector(
    pooled, test_repo_id, migrated_database, fake_model
):
    fake = FakeEmbedder()
    pool = {cid for cid, _, _ in embed_pool_rows(migrated_database, test_repo_id, fake)}
    plant_code_chunk(migrated_database, test_repo_id, pooled.sha["c1"])
    qvec = fake.encode_query(QUESTION)
    write_vectors(migrated_database, [(PLANTED, qvec, "e" * 64)])  # nearer than any pool document

    unfiltered = exact_search(qvec, limit=50, repo_id=test_repo_id, model_version=fake_model)
    assert unfiltered[0].chunk_id == PLANTED
    dense = DenseRetriever(test_repo_id, model_version=fake_model)
    found = dense.retrieve(QUESTION, qvec, 10)
    assert found and PLANTED not in found and set(found) <= pool
    assert found == dense.ranked(QUESTION, qvec)[:10]
    with pytest.raises(ValueError, match="frozen vector"):
        DenseRetriever(test_repo_id, model_version=fake_model).retrieve(QUESTION, None, 10)


def test_the_fused_row_returns_pool_documents_the_same_way_twice(
    pooled, test_repo_id, migrated_database, fake_model
):
    fake = FakeEmbedder()
    pool = {cid for cid, _, _ in embed_pool_rows(migrated_database, test_repo_id, fake)}
    qvec = fake.encode_query(QUESTION)

    def fused():
        rrf = RrfRetriever(
            Bm25Retriever(test_repo_id), DenseRetriever(test_repo_id, model_version=fake_model)
        )
        return rrf.retrieve(QUESTION, qvec, 10)

    first = fused()
    assert first and set(first) <= pool and fused() == first
