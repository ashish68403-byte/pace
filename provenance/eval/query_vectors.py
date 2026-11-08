"""Slice B5b: every golden question's frozen vector, baked once and committed.

`pace eval bake` embeds each distinct question exactly as `LocalEmbedder.encode_query`
embeds a live one: `query_text(question)`, bge's prefix once, one question at a time, so
no batch's padding touches a vector. It writes the flat .npz `FrozenEmbedder` reads, keyed
by `embedding_key`. Keys are sorted and every zip entry carries one fixed timestamp, so the
same vectors give the same bytes. The file is computed only from committed questions, so it
may be committed (DATA-PROTECTION §3).
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, Protocol

import numpy as np
from numpy.lib.format import write_array

from provenance.retrieve.embedder import embedding_key, query_text

#: The earliest date a zip entry can carry: a constant, so rewriting the file changes no byte.
ZIP_EPOCH: Final = (1980, 1, 1, 0, 0, 0)


class QueryEmbedder(Protocol):
    def encode_query(self, text: str) -> np.ndarray: ...


def bake(questions: Sequence[str], embedder: QueryEmbedder, model_id: str) -> dict[str, np.ndarray]:
    """{embedding_key(model, query_text(q)): float32 vector}, one per distinct question."""
    vectors: dict[str, np.ndarray] = {}
    for question in sorted(set(questions)):
        key = embedding_key(model_id, query_text(question))
        vectors[key] = np.asarray(embedder.encode_query(question), dtype=np.float32)
    return vectors


def write_npz(path: Path, vectors: Mapping[str, np.ndarray]) -> None:
    """A standard .npz that `np.load` reads, with sorted keys and fixed timestamps."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for key in sorted(vectors):
            buffer = io.BytesIO()
            write_array(buffer, np.ascontiguousarray(vectors[key]), allow_pickle=False)
            archive.writestr(zipfile.ZipInfo(f"{key}.npy", date_time=ZIP_EPOCH), buffer.getvalue())
