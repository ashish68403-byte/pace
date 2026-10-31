"""The gold-validity audit against pace-db, on history_repo (see conftest).

Its questions: `alpha` and `helper` added at c1 (commit message "init", no PR), and
`helper` changed at c7 (PR 14). Once the fake GitHub is fetched, PR 14 has a description
and one conversation comment.
"""

from __future__ import annotations

import pytest
from github_fakes import fake_github, fetch

from provenance.analysis.linkage_bias import linkage_bias
from provenance.eval.generator import generate
from provenance.eval.gold_validity import stratum_of
from provenance.eval.labelling import load_view
from provenance.eval.leakage import (
    PoolIndex,
    load_pool_from_db,
    reachable_at_k,
    resolve_gold_from_db,
)
from provenance.ingest.pr_pool import write_pr_pool

pytestmark = pytest.mark.db

ALPHA_ADDED = "Why was `alpha` added to `core/src/pkg/alpha.py`?"
HELPER_CHANGED = "Why did `helper` change in January 2020?"


@pytest.fixture
def fetched(pooled, test_repo_id, migrated_database):
    """The pool with PR text: the fake GitHub fetched, and the PR documents built."""
    fetch(test_repo_id, migrated_database, fake_github(pooled.sha)[0])
    write_pr_pool(migrated_database, test_repo_id)
    return pooled


def records_by_question(repo, conn, repo_id):
    return {r.question: r for r in generate(conn, repo_id, repo.root).records}


def test_a_pr_linked_view_shows_its_gold_and_the_functions_diff(
    fetched, test_repo_id, migrated_database
):
    record = records_by_question(fetched, migrated_database, test_repo_id)[HELPER_CHANGED]
    view = load_view(migrated_database, test_repo_id, fetched.root, record, stratum_of(record))
    assert view.stratum == "archaeology-pr" and view.committed_on == "2020-01-08"
    assert view.message.startswith("edit alpha after move (#14)")
    assert (view.pr, view.description) == (14, "PR 14\n\nHelper says three now.")
    assert (view.reviews, view.comments) == ((), ("Three, because the scheduler changed.",))
    assert "-    return 'one'" in view.change and "+    return 'three'" in view.change
    assert "return 2" not in view.change and "return 3" not in view.change  # helper alone


def test_a_commit_only_view_shows_the_message_and_the_source(
    fetched, test_repo_id, migrated_database
):
    record = records_by_question(fetched, migrated_database, test_repo_id)[ALPHA_ADDED]
    view = load_view(migrated_database, test_repo_id, fetched.root, record, stratum_of(record))
    assert view.stratum == "provenance-commit"
    assert (view.pr, view.description, view.comments) == (None, None, ())
    assert view.message.startswith("init")
    assert "def alpha():" in view.change and "return 1" in view.change


def test_no_lexical_baseline_reaches_the_fixtures_gold(pooled, test_repo_id, migrated_database):
    # Without PR text no pool document shares a word with both a question and its gold
    # (see test_golden_generation), so every record is out of reach even at k = 100.
    records = list(generate(migrated_database, test_repo_id, pooled.root).records)
    index = PoolIndex(load_pool_from_db(migrated_database, test_repo_id))
    gold = {
        r.qid: resolve_gold_from_db(r, migrated_database, test_repo_id).chunk_ids for r in records
    }
    assert len(records) == 3
    assert reachable_at_k(index, records, gold, 100) == {r.qid: False for r in records}


def test_linkage_bias_on_the_fixture(pooled, test_repo_id, migrated_database):
    # Qualifying pool commits: c2, c6 and c7 name a PR; c1, c3, c8 and c9 do not; m5 is a
    # merge. One author, every commit in January 2020, every file in core/src/pkg.
    summary = linkage_bias(
        migrated_database,
        test_repo_id,
        numbering_starts=pooled.sha["c1"],
        module_root="core/src/",
    )
    assert summary["groups"] == {"linked": 3, "no_reference": 4}
    assert summary["modules"] == {"linked": {"pkg": 1.0}, "no_reference": {"pkg": 1.0}}
    assert (summary["module_tvd"], summary["year_tvd"]) == (0.0, 0.0)
    assert summary["authors"]["linked"] == {"distinct": 1, "top10_share": 1.0}
    assert summary["authors"]["top10_overlap"] == 1
    assert summary["years"] == {"linked": {"2020": 3}, "no_reference": {"2020": 4}}
    assert "Fixture Author" not in str(summary)
