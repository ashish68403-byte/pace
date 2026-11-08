"""Slice C1: provenance-chain recall's per-question values (ADR-0005 §2-§3).

Arms A, C and L score 1 when the gold commit is among their commits. Arm B, the equal-size
control, scores the exact chance that a random subset of lineage's commits, as large as blame's
set, holds the gold: 3 of 8 is 0.375 when the gold is among the 8.
"""

from __future__ import annotations

import math

import pytest

from provenance.eval.metrics import chain_hit, equal_size_chance

LINEAGE = [f"c{i}" for i in range(8)]


def test_chain_hit_is_one_only_when_a_gold_commit_is_among_the_commits():
    assert chain_hit(["a", "b"], ["b"]) == 1.0
    assert chain_hit(["a", "b"], ["z"]) == 0.0
    assert chain_hit([], ["z"]) == 0.0


@pytest.mark.parametrize(
    ("blame_count", "lineage", "gold", "chance"),
    [
        (3, LINEAGE, ["c5"], 0.375),
        (3, LINEAGE, ["zz"], 0.0),
        (9, LINEAGE, ["c5"], 1.0),
        (2, [], ["c5"], 0.0),
        (0, LINEAGE, ["c5"], 0.0),
    ],
)
def test_the_equal_size_chance(blame_count, lineage, gold, chance):
    assert equal_size_chance(blame_count, lineage, gold) == pytest.approx(chance)


def test_two_gold_commits_use_the_exact_hypergeometric_chance():
    # A random 3 of 8 misses both golds in C(6, 3) of the C(8, 3) ways to choose.
    expected = 1 - math.comb(6, 3) / math.comb(8, 3)
    assert equal_size_chance(3, LINEAGE, ["c1", "c6"]) == pytest.approx(expected)


def test_a_negative_blame_count_is_refused():
    with pytest.raises(ValueError, match="blame_count"):
        equal_size_chance(-1, LINEAGE, ["c5"])
