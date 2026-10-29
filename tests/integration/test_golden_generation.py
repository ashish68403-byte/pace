"""Slice B3 against pace-db: the symbol walk, the golden set and the verdict on history_repo.

history_repo (see conftest): c1 adds `alpha`, `helper` and `B.grow`; c2 and c7 change
`alpha`, both in January 2020; c7 also changes `helper`; c8 deletes b.py and c9 re-adds it
with no functions. Exactly three questions survive the rules: `alpha` and `helper` added at
c1, and `helper` changed at c7 (PR 14).
"""

from __future__ import annotations

import pytest

from provenance.eval.candidates import walk_symbols

pytestmark = pytest.mark.db


def test_the_walk_sees_every_symbol_event(
    history_repo, test_repo_id, ingest, migrated_database, query
):
    ingest()
    walk = walk_symbols(migrated_database, test_repo_id, history_repo.root)
    files = dict(query("SELECT path, file_id FROM files WHERE repo_id = %s", test_repo_id))
    alpha, b = files["core/src/pkg/alpha.py"], files["core/src/pkg/b.py"]
    events = sorted(
        (e.template, e.qualified_name, history_repo.name_of(e.sha), e.path) for e in walk.events
    )
    assert events == [
        ("added", "B.grow", "c1", "core/src/pkg/b.py"),
        ("added", "alpha", "c1", "core/src/pkg/alpha.py"),
        ("added", "helper", "c1", "core/src/pkg/alpha.py"),
        ("changed", "alpha", "c2", "core/src/pkg/alpha.py"),
        ("changed", "alpha", "c7", "core/src/pkg/alpha.py"),
        ("changed", "helper", "c7", "core/src/pkg/alpha.py"),
    ]
    assert {e.month for e in walk.events} == {"2020-01"}
    assert walk.disappeared == {history_repo.sha["c8"]: {(b, "grow")}}
    assert walk.head == {alpha: frozenset({"alpha", "helper"}), b: frozenset()}
    assert (walk.revisions, walk.unparseable_blobs, walk.skipped_unknown_side) == (10, 0, 0)
