"""Slice B5b: a stored vector keeps float32's full precision."""

from __future__ import annotations

import numpy as np

from provenance.ingest.pool_embedding import GROUP, vector_literal


def test_a_vector_is_written_at_float32_precision():
    vector = np.random.default_rng(0).standard_normal(384).astype(np.float32)
    text = vector_literal(vector)
    back = np.array([float(x) for x in text[1:-1].split(",")], dtype=np.float32)
    assert text.startswith("[") and text.endswith("]")
    assert np.array_equal(back, vector)
    assert GROUP == 256
