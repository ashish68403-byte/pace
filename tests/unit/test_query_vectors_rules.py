"""Slice B5b: the golden questions' frozen vectors, baked the way a live query is embedded."""

from __future__ import annotations

import sys
import types

import numpy as np

from provenance.config import settings
from provenance.eval import runner
from provenance.eval.query_vectors import bake, write_npz
from provenance.retrieve.embedder import FrozenEmbedder, LocalEmbedder, embedding_key, query_text

MODEL = "pace-test-fake"
QUESTIONS = ["Why did `alpha` change in January 2020?", "Why was `beta` added to `pkg/b.py`?"]


class Fake:
    """Remembers what it is asked to embed; one fixed unit vector per text."""

    def __init__(self):
        self.asked = []

    def encode_query(self, text):
        self.asked.append(text)
        vector = np.random.default_rng(len(text)).standard_normal(384).astype(np.float32)
        return vector / np.linalg.norm(vector)


def test_each_distinct_question_is_embedded_once_and_keyed_with_its_prefix():
    fake = Fake()
    vectors = bake([*QUESTIONS, QUESTIONS[0]], fake, MODEL)
    assert sorted(fake.asked) == sorted(QUESTIONS)  # bare: encode_query adds the prefix itself
    assert set(vectors) == {embedding_key(MODEL, query_text(q)) for q in QUESTIONS}
    assert all(v.dtype == np.float32 and v.shape == (384,) for v in vectors.values())


def test_the_file_is_the_one_frozen_embedder_reads(tmp_path):
    vectors = bake(QUESTIONS, Fake(), MODEL)
    write_npz(tmp_path / "qvecs.npz", vectors)
    frozen = FrozenEmbedder(tmp_path / "qvecs.npz", model_id=MODEL)
    key = embedding_key(MODEL, query_text(QUESTIONS[0]))
    np.testing.assert_allclose(frozen.encode_query(QUESTIONS[0]), vectors[key], rtol=1e-6)
    records = [types.SimpleNamespace(qid=f"q{i}", question=q) for i, q in enumerate(QUESTIONS)]
    assert runner.missing_query_vectors(frozen, records) == []
    unseen = [types.SimpleNamespace(qid="new", question="Why did `gamma` change?")]
    assert runner.missing_query_vectors(frozen, unseen) == ["new"]


def test_the_same_vectors_write_the_same_bytes(tmp_path):
    vectors = bake(QUESTIONS, Fake(), MODEL)
    write_npz(tmp_path / "a.npz", vectors)
    write_npz(tmp_path / "b.npz", dict(reversed(list(vectors.items()))))
    assert (tmp_path / "a.npz").read_bytes() == (tmp_path / "b.npz").read_bytes()


def test_the_local_embedder_keeps_its_model_in_a_persistent_folder(monkeypatch):
    seen = {}

    class TextEmbedding:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    fake_fastembed = types.SimpleNamespace(TextEmbedding=TextEmbedding)
    monkeypatch.setitem(sys.modules, "fastembed", fake_fastembed)
    LocalEmbedder()._ensure()
    assert seen == {
        "model_name": f"BAAI/{settings.embedding_model_version}",
        "threads": 2,
        "cache_dir": str(settings.embedding_cache_dir),
    }
    assert settings.embedding_cache_dir.parts[-3:] == (".cache", "pace", "fastembed")
