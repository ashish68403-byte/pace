"""Fake vectors for the tests that need them (slice B5b).

CI never installs the `ml` extra, so no test runs a real model. `FakeEmbedder` hashes each
word to a fixed random direction in 384 dimensions and sums them: texts that share words
point the same way, which is all a test of storage, ranking or fusion needs. It writes under
its own model name, `pace-test-fake`, so its vectors never mix with real ones; the
`fake_model` fixture registers that name and deletes every vector written under it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Any

import numpy as np

from provenance.graph.tables import RATIONALE_KINDS
from provenance.retrieve.dense import _as_literal, resolve_code_embedding_table
from provenance.retrieve.embedder import query_text

FAKE_MODEL = "pace-test-fake"
DIM = 384
_WORDS = re.compile(r"[a-z0-9]+")


def _direction(word: str) -> np.ndarray:
    seed = int.from_bytes(hashlib.sha256(word.encode()).digest()[:8], "little")
    return np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)


class FakeEmbedder:
    model_id = FAKE_MODEL
    #: Small on purpose, so a four-word commit message already counts as cut.
    max_tokens = 3

    def __init__(self) -> None:
        self.asked: list[str] = []

    def vector(self, text: str) -> np.ndarray:
        words = _WORDS.findall(text.lower())
        if not words:
            unit = np.zeros(DIM, dtype=np.float32)
            unit[0] = 1.0
            return unit
        total = np.sum([_direction(word) for word in words], axis=0).astype(np.float32)
        return total / np.linalg.norm(total)

    def encode_documents(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, DIM), dtype=np.float32)
        return np.vstack([self.vector(text) for text in texts])

    def encode_query(self, text: str) -> np.ndarray:
        self.asked.append(text)
        return self.vector(query_text(text))

    def token_lengths(self, texts: Sequence[str]) -> list[int]:
        return [len(_WORDS.findall(text.lower())) for text in texts]

    def describe(self) -> dict[str, Any]:
        return {"model": FAKE_MODEL, "source": "tests/integration/vector_fakes.py"}


def pool_rows(conn: Any, repo_id: int) -> list[tuple[str, str, str]]:
    """(chunk_id, content, content_sha) for the repository's pool, by chunk_id."""
    return list(
        conn.execute(
            "SELECT chunk_id, content, content_sha FROM chunks "
            "WHERE repo_id = %s AND kind = ANY(%s::artifact_kind[]) ORDER BY chunk_id",
            (repo_id, list(RATIONALE_KINDS)),
        ).fetchall()
    )


def write_vectors(
    conn: Any, rows: Sequence[tuple[str, np.ndarray, str]], model: str = FAKE_MODEL
) -> None:
    """(chunk_id, vector, content_sha) rows, into the table `code_current` names."""
    table = resolve_code_embedding_table(conn)
    for chunk_id, vector, content_sha in rows:
        conn.execute(
            f"INSERT INTO {table} (chunk_id, model_version, embedding, content_sha) "
            "VALUES (%s, %s, %s::vector, %s)",
            (chunk_id, model, _as_literal(vector), content_sha),
        )


def embed_pool_rows(conn: Any, repo_id: int, embedder: FakeEmbedder) -> list[tuple[str, str, str]]:
    """A fake vector for every pool document; returns the pool's rows."""
    rows = pool_rows(conn, repo_id)
    vectors = embedder.encode_documents([content for _, content, _ in rows])
    write_vectors(conn, [(cid, vec, sha) for (cid, _, sha), vec in zip(rows, vectors, strict=True)])
    return rows
