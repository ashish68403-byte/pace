"""The census's arithmetic, without a database."""

from __future__ import annotations

from provenance.analysis.pool_census import percentile


def test_percentile_is_nearest_rank_like_percentile_disc() -> None:
    values = list(range(1, 11))
    assert [percentile(values, q) for q in (0.5, 0.9, 0.99)] == [5, 9, 10]
    assert percentile([7], 0.5) == 7
