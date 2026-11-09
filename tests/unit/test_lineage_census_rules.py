"""Slice C3a: the agreement check's sample and its score, fixed before any lineage number."""

from __future__ import annotations

import hashlib

from provenance.analysis.lineage_census import AGREEMENT_SALT, agreement_sample
from provenance.eval.metrics import set_f1


def test_set_f1():
    assert set_f1({"a", "b"}, {"a", "b"}) == 1.0
    assert set_f1(set(), set()) == 1.0
    assert set_f1({"a"}, {"b"}) == 0.0
    assert set_f1({"a", "b", "c", "d"}, {"a", "b", "c", "e"}) == 0.75


def test_the_sample_takes_every_stop_then_fills_with_reaches_and_orders_by_salted_hash():
    functions = [(f"p{i}.py", f"f{i}") for i in range(120)]
    groups = {
        "moved_stops": functions[:3],
        "moved_reaches": functions[3:60],
        "other": functions[60:],
    }
    sample = agreement_sample(groups, size=50)

    def order(items):
        return sorted(
            items,
            key=lambda f: hashlib.sha256(f"{AGREEMENT_SALT}{f[0]}::{f[1]}".encode()).hexdigest(),
        )

    assert sample["moved_stops"] == order(groups["moved_stops"])
    assert sample["moved_reaches"] == order(groups["moved_reaches"])[:47]
    assert sample["other"] == order(groups["other"])[:50]
