"""Slice B3 against pace-db: the symbol walk, the golden set and the verdict on history_repo.

history_repo (see conftest): c1 adds `alpha`, `helper` and `B.grow`; c2 and c7 change
`alpha`, both in January 2020; c7 also changes `helper`; c8 deletes b.py and c9 re-adds it
with no functions. Exactly three questions survive the rules: `alpha` and `helper` added at
c1, and `helper` changed at c7 (PR 14).
"""

from __future__ import annotations

import pytest

from provenance.eval.candidates import walk_symbols
from provenance.eval.generator import (
    RANK_1,
    RARE_TOKEN,
    UNMEASURED,
    GenerationInvariantError,
    NothingToGenerate,
    generate,
    write_outputs,
)
from provenance.eval.schema import assign_splits, group_key, load_golden_set, passes_leakage_filter
from provenance.ingest.message_pool import write_pool

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


ALPHA_ADDED = "Why was `alpha` added to `core/src/pkg/alpha.py`?"
HELPER_ADDED = "Why was `helper` added to `core/src/pkg/alpha.py`?"
HELPER_CHANGED = "Why did `helper` change in January 2020?"


def _generated(history_repo, test_repo_id, ingest, conn):
    ingest()
    write_pool(conn, test_repo_id, numbering_starts=history_repo.sha["c1"])
    return generate(conn, test_repo_id, history_repo.root)


def test_the_fixture_yields_exactly_three_questions(
    history_repo, test_repo_id, ingest, migrated_database
):
    generation = _generated(history_repo, test_repo_id, ingest, migrated_database)
    sha = history_repo.sha
    evidence = {r.question: r.required_evidence.keys() for r in generation.records}
    assert evidence == {
        ALPHA_ADDED: [("commit", sha["c1"])],
        HELPER_ADDED: [("commit", sha["c1"])],
        HELPER_CHANGED: [("commit", sha["c7"]), ("pr", "14")],
    }
    qids = [r.qid for r in generation.records]
    assert qids == sorted(qids) and len(set(qids)) == 3
    anchors = {
        (a.path, a.qualified_name)
        for r in generation.records
        for a in r.supporting_evidence.symbol_anchors
    }
    assert anchors == {("core/src/pkg/alpha.py", "alpha"), ("core/src/pkg/alpha.py", "helper")}


def test_every_fixture_question_is_retained_with_its_facts(
    history_repo, test_repo_id, ingest, migrated_database
):
    generation = _generated(history_repo, test_repo_id, ingest, migrated_database)
    assert all(passes_leakage_filter(r) for r in generation.records)
    df = {r.question: r.gold_symbol_df for r in generation.records}
    assert df == {ALPHA_ADDED: 2, HELPER_ADDED: 0, HELPER_CHANGED: 0}
    # No pool document shares a word with "init" or with c7's message, so neither
    # baseline retrieves gold. Counting documents that matched nothing would have put
    # gold in every top 10 of this 8-document pool.
    hits = {(r.baseline_grep_r10, r.baseline_bm25_r10) for r in generation.records}
    assert hits == {(0.0, 0.0)}
    versions = {r.leakage.probe_index_version for r in generation.records if r.leakage}
    assert versions == {"commit-message-v1:8"}
    assert {r.corpus_commit for r in generation.records} == {history_repo.sha["c9"]}


def test_the_split_follows_each_records_group(
    history_repo, test_repo_id, ingest, migrated_database
):
    generation = _generated(history_repo, test_repo_id, ingest, migrated_database)
    for record in generation.records:
        prs = record.required_evidence.pr_numbers
        group = group_key(record.required_evidence.commit_shas[0], prs[0] if prs else None)
        assert record.split == assign_splits([group])[group]


def test_the_census_counts_every_rule_and_outcome(
    history_repo, test_repo_id, ingest, migrated_database
):
    census = _generated(history_repo, test_repo_id, ingest, migrated_database).census
    assert census["rules"] == {
        "added": {"raw": 3, "at_head": 2, "not_a_move": 2, "added_once": 2},
        "changed": {"raw": 3, "at_head": 3, "one_commit_per_month": 1, "unique_name": 1},
    }
    assert census["retention"]["all"] == {
        "candidates": 3,
        "retained": 3,
        RANK_1: 0,
        RARE_TOKEN: 0,
        UNMEASURED: 0,
    }
    assert census["walk"]["revisions"] == 10
    assert census["corpus_commit"] == history_repo.sha["c9"]


def test_two_runs_write_the_same_bytes(
    history_repo, test_repo_id, ingest, migrated_database, tmp_path
):
    first = _generated(history_repo, test_repo_id, ingest, migrated_database)
    second = generate(migrated_database, test_repo_id, history_repo.root)
    write_outputs(first, tmp_path / "1.jsonl", tmp_path / "1.json")
    write_outputs(second, tmp_path / "2.jsonl", tmp_path / "2.json")
    assert (tmp_path / "1.jsonl").read_bytes() == (tmp_path / "2.jsonl").read_bytes()
    assert (tmp_path / "1.json").read_bytes() == (tmp_path / "2.json").read_bytes()
    assert [r.qid for r in load_golden_set(tmp_path / "1.jsonl")] == [r.qid for r in first.records]


def test_a_corpus_at_another_commit_is_refused(
    history_repo, test_repo_id, ingest, migrated_database
):
    ingest(rev=history_repo.sha["c7"])
    write_pool(migrated_database, test_repo_id, numbering_starts=history_repo.sha["c1"])
    with pytest.raises(GenerationInvariantError, match="never saw"):
        generate(migrated_database, test_repo_id, history_repo.root)


def test_without_a_pool_there_is_nothing_to_generate(
    history_repo, test_repo_id, ingest, migrated_database
):
    ingest()
    with pytest.raises(NothingToGenerate, match="pace pool build"):
        generate(migrated_database, test_repo_id, history_repo.root)
