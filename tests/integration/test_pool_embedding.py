"""Slice B5b against pace-db: `pace pool embed` writes, resumes, refreshes and records."""

from __future__ import annotations

import pytest
from vector_fakes import FakeEmbedder, pool_rows

from provenance.ingest.pool_embedding import (
    EmbeddingIncomplete,
    build_record,
    count_missing,
    embed_pool,
    embedding_table,
    fingerprint,
    vector_literal,
)

pytestmark = pytest.mark.db


def stored(conn, repo_id, model):
    table = embedding_table(conn)
    return conn.execute(
        f"SELECT e.chunk_id, e.content_sha, c.content_sha FROM {table} e "
        "JOIN chunks c ON c.chunk_id = e.chunk_id WHERE c.repo_id = %s AND e.model_version = %s",
        (repo_id, model),
    ).fetchall()


def test_every_pool_document_gets_one_vector_with_its_content_sha(
    pooled, test_repo_id, migrated_database, fake_model
):
    pool = pool_rows(migrated_database, test_repo_id)
    assert embed_pool(migrated_database, test_repo_id, FakeEmbedder()) == len(pool) > 0
    rows = stored(migrated_database, test_repo_id, fake_model)
    assert sorted(cid for cid, _, _ in rows) == sorted(cid for cid, _, _ in pool)
    assert all(mine == chunks for _, mine, chunks in rows)
    assert count_missing(migrated_database, test_repo_id, fake_model) == 0
    assert embed_pool(migrated_database, test_repo_id, FakeEmbedder()) == 0  # nothing left


def test_a_stopped_run_resumes_without_duplicates(
    pooled, test_repo_id, migrated_database, fake_model
):
    total = len(pool_rows(migrated_database, test_repo_id))
    assert embed_pool(migrated_database, test_repo_id, FakeEmbedder(), group=2, stop_after=1) == 2
    assert count_missing(migrated_database, test_repo_id, fake_model) == total - 2
    assert embed_pool(migrated_database, test_repo_id, FakeEmbedder(), group=2) == total - 2
    assert len(stored(migrated_database, test_repo_id, fake_model)) == total


def test_a_new_document_and_a_stale_vector_are_embedded_on_the_next_run(
    pooled, test_repo_id, migrated_database, fake_model
):
    embed_pool(migrated_database, test_repo_id, FakeEmbedder())
    first = pool_rows(migrated_database, test_repo_id)[0][0]
    migrated_database.execute(
        f"UPDATE {embedding_table(migrated_database)} SET content_sha = %s "
        "WHERE chunk_id = %s AND model_version = %s",
        ("0" * 64, first, fake_model),
    )
    sha = pooled.sha["c1"]
    migrated_database.execute(
        "INSERT INTO chunks (chunk_id, repo_id, path, kind, language, start_line, end_line, "
        "content, content_sha, chunking_strategy_version, first_seen_commit, last_seen_commit) "
        "VALUES (%s, %s, %s, 'commit', 'text', 1, 1, 'a new commit message', %s, 'test', %s, %s)",
        ("d" * 64, test_repo_id, f"commit/{'d' * 40}", "c" * 64, sha, sha),
    )
    assert count_missing(migrated_database, test_repo_id, fake_model) == 2
    assert embed_pool(migrated_database, test_repo_id, FakeEmbedder()) == 2
    assert count_missing(migrated_database, test_repo_id, fake_model) == 0


def test_the_record_names_the_model_and_its_fingerprint_moves_with_any_vector(
    pooled, test_repo_id, migrated_database, fake_model
):
    fake = FakeEmbedder()
    with pytest.raises(EmbeddingIncomplete, match="no current vector"):
        build_record(migrated_database, test_repo_id, fake, seconds=0.0, embedded=0)
    embed_pool(migrated_database, test_repo_id, fake)
    record = build_record(migrated_database, test_repo_id, fake, seconds=1.5, embedded=7)
    pool = pool_rows(migrated_database, test_repo_id)
    cut = sum(n > fake.max_tokens for n in fake.token_lengths([c for _, c, _ in pool]))
    assert record["model"]["model"] == fake_model and record["vectors"] == len(pool)
    assert record["fingerprint"] == fingerprint(migrated_database, test_repo_id, fake_model)[0]
    assert sum(record["documents"].values()) == len(pool)
    assert sum(record["truncated"].values()) == cut
    assert record["this_run"] == {"embedded": 7, "seconds": 1.5}
    migrated_database.execute(
        f"UPDATE {embedding_table(migrated_database)} SET embedding = %s::vector "
        "WHERE chunk_id = %s AND model_version = %s",
        (vector_literal(fake.vector("something else entirely")), pool[0][0], fake_model),
    )
    assert fingerprint(migrated_database, test_repo_id, fake_model)[0] != record["fingerprint"]
