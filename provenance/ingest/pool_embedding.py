"""Slice B5b: the pool's document vectors, where dense retrieval reads them.

`pace pool embed` embeds every pool document that has no vector for the configured model
into the table the `code_current` alias names. ADR-0003's 2026-10-28 erratum sends pool
documents through `embeddings_code_*`: they are chunks, keyed on chunk_id, and the
`doc_ref`-keyed prose tables stay unused.

* **The text** is the document as stored, with no prefix: bge's prefix is for queries.
* **Order** is by length, then chunk_id, in groups of GROUP documents, each committed on
  its own. A batch then holds similar lengths, which wastes less padding, and a document
  always lands in the same batch: a stopped run resumes on the same boundaries and writes
  the same vectors.
* **The record**, `results/pool_embedding.json`, says what produced the vectors, how many
  documents each kind has and how many ran past the model's window, and gives one
  fingerprint over every vector. It holds no text, and the vectors themselves never leave
  pace-db (DATA-PROTECTION §3).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

import numpy as np

from provenance.config import settings
from provenance.graph.tables import (
    CHUNKS,
    COL_CHUNK_ID,
    COL_REPO_ID,
    COL_TOMBSTONE,
    RATIONALE_KINDS,
)

SCHEMA: Final = "pace.pool_embedding/v1"
#: Documents per transaction: a stopped run loses at most one group.
GROUP: Final = 256
DEFAULT_RECORD: Final = Path("results") / "pool_embedding.json"
DRY_RUN_SEED: Final = 20261108

_POOL = (
    f"c.{COL_REPO_ID} = %(repo)s AND c.kind = ANY(%(kinds)s::artifact_kind[]) "
    f"AND c.{COL_TOMBSTONE} IS NULL"
)

_UPSERT = """
INSERT INTO {table} (chunk_id, model_version, embedding, content_sha)
VALUES (%s, %s, %s::vector, %s)
ON CONFLICT (chunk_id, model_version) DO UPDATE
    SET embedding = EXCLUDED.embedding,
        content_sha = EXCLUDED.content_sha,
        embedded_at = now()
"""


class DocumentEmbedder(Protocol):
    """What `pace pool embed` needs: `LocalEmbedder`, or the tests' fake."""

    model_id: str
    max_tokens: int

    def encode_documents(self, texts: Sequence[str]) -> np.ndarray: ...

    def token_lengths(self, texts: Sequence[str]) -> list[int]: ...

    def describe(self) -> dict[str, Any]: ...


class EmbeddingIncomplete(RuntimeError):
    """Some pool document has no current vector, so no record can describe the pool."""


@dataclass(frozen=True)
class PoolDocument:
    chunk_id: str
    kind: str
    content: str
    content_sha: str


def vector_literal(vector: np.ndarray) -> str:
    """pgvector's text form at float32's full precision: nine digits round-trip exactly."""
    values = np.asarray(vector, dtype=np.float32).ravel()
    return "[" + ",".join(format(float(x), ".9g") for x in values) + "]"


def embedding_table(conn: Any) -> str:
    """The physical table behind `code_current`, checked against the allowlist."""
    from provenance.retrieve.dense import resolve_code_embedding_table  # noqa: PLC0415

    return resolve_code_embedding_table(conn)


def _params(repo_id: int, model: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"repo": repo_id, "kinds": list(RATIONALE_KINDS)}
    if model is not None:
        params["model"] = model
    return params


def _pending(table: str, select: str) -> str:
    """Pool documents with no vector for the model, or one written for other text."""
    return f"""
SELECT {select}
FROM {CHUNKS} c
LEFT JOIN {table} e ON e.{COL_CHUNK_ID} = c.{COL_CHUNK_ID} AND e.model_version = %(model)s
WHERE {_POOL}
  AND (e.{COL_CHUNK_ID} IS NULL OR e.content_sha IS DISTINCT FROM c.content_sha)
"""


def pending(conn: Any, repo_id: int, model: str) -> list[PoolDocument]:
    """The documents a run must embed, by length and then chunk_id."""
    columns = f"c.{COL_CHUNK_ID}, c.kind::text, c.content, c.content_sha"
    sql = _pending(embedding_table(conn), columns) + f"ORDER BY length(c.content), c.{COL_CHUNK_ID}"
    return [PoolDocument(*row) for row in conn.execute(sql, _params(repo_id, model)).fetchall()]


def count_missing(conn: Any, repo_id: int, model: str) -> int:
    """How many pool documents have no current vector for the model."""
    sql = _pending(embedding_table(conn), "count(*)")
    return int(conn.execute(sql, _params(repo_id, model)).fetchone()[0])


def embed_pool(
    conn: Any,
    repo_id: int,
    embedder: DocumentEmbedder,
    *,
    group: int = GROUP,
    stop_after: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> int:
    """Embed every pending pool document, one committed group at a time; returns how many.

    `stop_after` ends the run after that many groups: how the tests stop a run part way.
    """
    table = embedding_table(conn)
    docs = pending(conn, repo_id, embedder.model_id)
    conn.commit()
    started = time.monotonic()
    written = 0
    for number, start in enumerate(range(0, len(docs), group), start=1):
        if stop_after is not None and number > stop_after:
            break
        batch = docs[start : start + group]
        vectors = embedder.encode_documents([doc.content for doc in batch])
        rows = [
            (doc.chunk_id, embedder.model_id, vector_literal(vector), doc.content_sha)
            for doc, vector in zip(batch, vectors, strict=True)
        ]
        with conn.cursor() as cur:
            cur.executemany(_UPSERT.format(table=table), rows)
        conn.commit()
        written += len(batch)
        if progress is not None and (number % 20 == 0 or written == len(docs)):
            rate = written / max(time.monotonic() - started, 1e-9)
            progress(f"  embedded {written:,} of {len(docs):,} ({rate:.1f} a second)")
    return written


def fingerprint(conn: Any, repo_id: int, model: str) -> tuple[str, int]:
    """One SHA-256 over every pool document's chunk_id and stored vector, in chunk_id order.

    Each vector is hashed in the database, over pgvector's text form, so 70,000 of them cost
    one small fetch. A changed vector, or a document gained or lost, changes the result.
    """
    table = embedding_table(conn)
    rows = conn.execute(
        f"""
SELECT e.{COL_CHUNK_ID}, encode(sha256(convert_to(e.embedding::text, 'UTF8')), 'hex')
FROM {table} e
JOIN {CHUNKS} c ON c.{COL_CHUNK_ID} = e.{COL_CHUNK_ID}
WHERE {_POOL} AND e.model_version = %(model)s
ORDER BY e.{COL_CHUNK_ID}
""",
        _params(repo_id, model),
    ).fetchall()
    digest = hashlib.sha256(f"{model}\n".encode())
    for chunk_id, vector_digest in rows:
        digest.update(f"{chunk_id}:{vector_digest}\n".encode())
    return digest.hexdigest(), len(rows)


def lengths_by_kind(
    conn: Any, repo_id: int, embedder: DocumentEmbedder
) -> tuple[dict[str, int], dict[str, int]]:
    """Documents per kind, and how many of each run past the model's window."""
    rows = conn.execute(
        f"SELECT c.kind::text, c.content FROM {CHUNKS} c WHERE {_POOL} ORDER BY c.{COL_CHUNK_ID}",
        _params(repo_id),
    ).fetchall()
    documents: Counter[str] = Counter()
    truncated: Counter[str] = Counter()
    for start in range(0, len(rows), 1000):
        batch = rows[start : start + 1000]
        lengths = embedder.token_lengths([content for _, content in batch])
        for (kind, _), length in zip(batch, lengths, strict=True):
            documents[kind] += 1
            truncated[kind] += int(length > embedder.max_tokens)
    return dict(sorted(documents.items())), {kind: truncated[kind] for kind in sorted(documents)}


def build_record(
    conn: Any, repo_id: int, embedder: DocumentEmbedder, *, seconds: float, embedded: int
) -> dict[str, Any]:
    """`results/pool_embedding.json`. Refuses unless every pool document has its vector."""
    from provenance.eval.generator import pool_version  # noqa: PLC0415

    missing = count_missing(conn, repo_id, embedder.model_id)
    if missing:
        raise EmbeddingIncomplete(
            f"{missing:,} pool documents have no current vector for {embedder.model_id}: "
            "run `pace pool embed` again"
        )
    digest, vectors = fingerprint(conn, repo_id, embedder.model_id)
    documents, truncated = lengths_by_kind(conn, repo_id, embedder)
    return {
        "schema": SCHEMA,
        "model": embedder.describe(),
        "pool_version": pool_version(conn, repo_id),
        "documents": documents,
        "max_tokens": embedder.max_tokens,
        "truncated": truncated,
        "vectors": vectors,
        "fingerprint": digest,
        "this_run": {"embedded": embedded, "seconds": round(seconds, 1)},
    }


def render_record(record: dict[str, Any], path: Path) -> str:
    total = sum(record["documents"].values())
    cut = sum(record["truncated"].values())
    per_kind = ", ".join(
        f"{kind} {n:,} ({record['truncated'][kind]:,} cut)"
        for kind, n in record["documents"].items()
    )
    share = f"{cut / total:.1%}" if total else "n/a"
    return "\n".join(
        [
            f"pool embedded with {record['model']['model']} ({record['model'].get('source')}): "
            f"{record['vectors']:,} vectors, fingerprint {record['fingerprint'][:12]}",
            f"  {per_kind}",
            f"  {cut:,} of {total:,} documents ({share}) ran past the "
            f"{record['max_tokens']}-token window",
            f"  this run embedded {record['this_run']['embedded']:,} in "
            f"{record['this_run']['seconds'] / 60:.1f} min",
            f"  wrote {path}",
        ]
    )


def dry_run(conn: Any, repo_id: int, embedder: DocumentEmbedder, n: int) -> str:
    """Embed a seeded random sample, write nothing, and say how long the whole pool takes."""
    rows = conn.execute(
        f"SELECT c.{COL_CHUNK_ID}, c.kind::text, c.content, c.content_sha "
        f"FROM {CHUNKS} c WHERE {_POOL} ORDER BY c.{COL_CHUNK_ID}",
        _params(repo_id),
    ).fetchall()
    docs = [PoolDocument(*row) for row in rows]
    sample = random.Random(DRY_RUN_SEED).sample(docs, min(n, len(docs)))
    sample.sort(key=lambda doc: (len(doc.content), doc.chunk_id))  # as the real run batches
    embedder.encode_documents(["warm up"])  # the model's download and load stay untimed
    started = time.monotonic()
    for start in range(0, len(sample), GROUP):
        embedder.encode_documents([doc.content for doc in sample[start : start + GROUP]])
    seconds = max(time.monotonic() - started, 1e-9)
    rate = len(sample) / seconds
    return (
        f"dry run: {len(sample):,} documents in {seconds:.1f}s, {rate:.1f} a second; the whole "
        f"pool ({len(docs):,}) would take about {len(docs) / rate / 60:.0f} minutes. "
        "Nothing was written."
    )


def main(argv: Sequence[str] | None = None) -> int:
    """`pace pool embed`: embed the pool, then write its record."""
    parser = argparse.ArgumentParser(
        prog="pace pool embed",
        description="Embed every pool document into the table dense retrieval reads (B5b).",
    )
    parser.add_argument(
        "--dry-run",
        type=int,
        metavar="N",
        help="embed a seeded random sample of N documents, write nothing, and print the rate",
    )
    parser.add_argument(
        "--record", type=Path, default=DEFAULT_RECORD, help="where to write the record"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    from provenance.graph.db import connection  # noqa: PLC0415
    from provenance.graph.repos import UnknownRepository, resolve_repo_id  # noqa: PLC0415
    from provenance.retrieve.embedder import LocalEmbedder  # noqa: PLC0415

    embedder = LocalEmbedder()
    try:
        with connection() as conn:
            repo_id = resolve_repo_id(conn, settings.repo_owner, settings.repo_name)
            if args.dry_run:
                print(dry_run(conn, repo_id, embedder, args.dry_run))
                return 0
            started = time.monotonic()
            written = embed_pool(
                conn, repo_id, embedder, progress=lambda line: print(line, flush=True)
            )
            record = build_record(
                conn, repo_id, embedder, seconds=time.monotonic() - started, embedded=written
            )
    except UnknownRepository as exc:
        print(f"nothing to embed: {exc}", file=sys.stderr)
        return 2
    except EmbeddingIncomplete as exc:
        print(f"EMBEDDING INCOMPLETE: {exc}", file=sys.stderr)
        return 1
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(render_record(record, args.record))
    return 0
