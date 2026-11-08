"""Checkpoint B's ablation end to end against pace-db: six rows, every check, the same bytes."""

from __future__ import annotations

import json

import pytest
from vector_fakes import FAKE_MODEL, FakeEmbedder

from provenance.eval.ablation import run_ablation
from provenance.eval.generator import generate
from provenance.eval.leakage import load_pool_from_db, resolve_gold_from_db, run_probe
from provenance.eval.query_vectors import bake, write_npz
from provenance.eval.schema import write_golden_set
from provenance.ingest.pool_embedding import build_record, embed_pool, embedding_table

pytestmark = pytest.mark.db

ROWS = {"grep", "bm25_ident", "bm25_phrase", "bm25", "dense", "rrf"}


def ablation_inputs(pooled, repo_id, conn, tmp_path):
    """Every file `pace eval ablation` reads, built over history_repo with the fake embedder."""
    records = [
        r.model_copy(update={"split": "dev"}) for r in generate(conn, repo_id, pooled.root).records
    ]
    golden = write_golden_set(records, tmp_path / "golden.jsonl")
    pool = load_pool_from_db(conn, repo_id)
    verdict = run_probe(records, pool, lambda r: resolve_gold_from_db(r, conn, repo_id))
    (tmp_path / "verdict.json").write_text(json.dumps(verdict), encoding="utf-8")
    embedder = FakeEmbedder()
    embed_pool(conn, repo_id, embedder)
    record = build_record(conn, repo_id, embedder, seconds=0.0, embedded=0)
    (tmp_path / "embedding.json").write_text(json.dumps(record), encoding="utf-8")
    write_npz(tmp_path / "qvecs.npz", bake([r.question for r in records], embedder, FAKE_MODEL))
    inputs = {
        "golden": golden,
        "verdict": tmp_path / "verdict.json",
        "audit": tmp_path / "no-audit.json",
        "embedding_record": tmp_path / "embedding.json",
        "fixture": tmp_path / "qvecs.npz",
        "model_version": FAKE_MODEL,
        "repo_id": repo_id,
    }
    return records, record, inputs


def test_the_ablation_writes_six_rows_and_the_same_bytes_twice(
    pooled, test_repo_id, migrated_database, fake_model, tmp_path
):
    records, record, inputs = ablation_inputs(pooled, test_repo_id, migrated_database, tmp_path)
    written = []
    for i in (1, 2):
        out, per_query = tmp_path / f"ablation{i}.json", tmp_path / f"per_query{i}.jsonl"
        assert run_ablation(out=out, per_query_out=per_query, **inputs) == 0
        written.append((out.read_bytes(), per_query.read_bytes()))
    assert written[0] == written[1]
    table = json.loads(written[0][0])
    assert table["schema"] == "pace.ablation/v2" and set(table["rows"]) == ROWS
    assert set(table["pairs"]) == {"bm25_vs_bm25_phrase", "rrf_vs_bm25", "rrf_vs_dense"}
    sweep = table["rrf_k_sensitivity"]
    assert [p["k"] for p in sweep["points"]] == [10, 20, 30, 60, 100, 200]
    at_60 = next(p for p in sweep["points"] if p["k"] == 60)
    assert at_60["hits@10"] == table["rows"]["rrf"]["hits@10"]  # the sweep's k = 60 is the row
    assert table["embedding"]["fingerprint"] == record["fingerprint"]
    lines = [json.loads(line) for line in written[0][1].splitlines()]
    assert len(lines) == len(records) and set(lines[0]["first_gold_rank"]) == ROWS


def test_the_ablation_writes_nothing_when_a_check_fails(
    pooled, test_repo_id, migrated_database, fake_model, tmp_path
):
    records, record, inputs = ablation_inputs(pooled, test_repo_id, migrated_database, tmp_path)
    out, per_query = tmp_path / "ablation.json", tmp_path / "per_query.jsonl"

    def refused(**changes):
        return run_ablation(out=out, per_query_out=per_query, **{**inputs, **changes}) == 1

    # A record whose fingerprint is not the stored vectors'.
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps({**record, "fingerprint": "0" * 64}), encoding="utf-8")
    assert refused(embedding_record=tampered)

    # A scored question with no frozen vector.
    partial = tmp_path / "partial.npz"
    others = [r.question for r in records if r.question != records[0].question]
    write_npz(partial, bake(others, FakeEmbedder(), FAKE_MODEL))
    assert refused(fixture=partial)

    # A pool document with no vector; then, written back, one whose vector is for other text.
    table = embedding_table(migrated_database)
    first, second = (
        row[0]
        for row in migrated_database.execute(
            f"SELECT chunk_id FROM {table} WHERE model_version = %s ORDER BY chunk_id LIMIT 2",
            (FAKE_MODEL,),
        ).fetchall()
    )
    migrated_database.execute(
        f"DELETE FROM {table} WHERE chunk_id = %s AND model_version = %s", (first, FAKE_MODEL)
    )
    assert refused()
    embed_pool(migrated_database, test_repo_id, FakeEmbedder())  # the same vector, written back
    assert run_ablation(out=out, per_query_out=per_query, **inputs) == 0  # whole again
    out.unlink()
    per_query.unlink()
    migrated_database.execute(
        f"UPDATE {table} SET content_sha = %s WHERE chunk_id = %s AND model_version = %s",
        ("0" * 64, second, FAKE_MODEL),
    )
    assert refused()
    assert not out.exists() and not per_query.exists()
