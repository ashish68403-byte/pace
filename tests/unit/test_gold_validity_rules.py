"""ADR-0004's audit, the pure half: the sample, staleness, label files and the report."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date, timedelta

import pytest

from provenance.eval.gold_validity import (
    AuditError,
    AuditStale,
    LabelRecord,
    SampleExists,
    append_label,
    check_fresh,
    check_relabel_allowed,
    corpus_commit_of,
    draw_sample,
    load_labels,
    load_sample,
    population,
    stratum_of,
    write_sample,
)
from provenance.eval.schema import GoldenRecord

VERSION = "rationale-v1:commit=8"
CORPUS = "c" * 40
SMALL = {"provenance-pr": 2, "archaeology-pr": 3, "provenance-commit": 2, "archaeology-commit": 2}


def record(qid, cls, *, pr, split="dev", rank_1=False, handwritten=False, version=VERSION):
    commit = hashlib.sha1(qid.encode()).hexdigest()
    return GoldenRecord.model_validate(
        {
            "qid": qid,
            "query_class": cls,
            "split": split,
            "question": f"Why did `f_{qid}` change in March 2023?",
            "required_evidence": {
                "commit_shas": [commit],
                "pr_numbers": [] if pr is None else [pr],
            },
            "supporting_evidence": {
                "symbol_anchors": [{"path": "pkg/m.py", "qualified_name": f"f_{qid}"}]
            },
            "generation_strategy": "handwritten_test" if handwritten else "ast_diff_v0",
            "leakage": {
                "baseline_grep_r10": 0.0,
                "baseline_bm25_r10": 0.0,
                "gold_symbol_df": 1,
                "grep_answers_at_rank_1": rank_1,
                "rare_tokens_shared_with_gold": [],
                "probe_index_version": version,
                "probe_corpus_commit": CORPUS,
            },
            "corpus_commit": CORPUS,
        }
    )


def golden():
    """Four strata with room to spare, and three records the population leaves out."""
    records = [record(f"pp{i}", "provenance", pr=100 + i) for i in range(5)]
    records += [record(f"ap{i}", "archaeology", pr=200 + i) for i in range(6)]
    records += [record(f"pc{i}", "provenance", pr=None) for i in range(3)]
    records += [record(f"ac{i}", "archaeology", pr=None) for i in range(3)]
    records.append(record("test0", "archaeology", pr=300, split="test"))
    records.append(record("drop0", "archaeology", pr=301, rank_1=True))
    records.append(record("hand0", "archaeology", pr=302, handwritten=True))
    return records


def drawn(records=None):
    return draw_sample(
        records or golden(),
        pool_version=VERSION,
        corpus_commit=CORPUS,
        allocation=SMALL,
        relabel_size=3,
    )


def h(seed, qid):
    return int.from_bytes(hashlib.sha256(f"{seed}:{qid}".encode()).digest(), "big")


def test_the_population_is_retained_generated_dev_records():
    qids = {r.qid for r in population(golden())}
    assert len(qids) == 17 and not {"test0", "drop0", "hand0"} & qids


def test_a_stratum_is_query_class_by_pr_link():
    assert stratum_of(record("x", "provenance", pr=5)) == "provenance-pr"
    assert stratum_of(record("y", "archaeology", pr=None)) == "archaeology-commit"


def test_each_stratum_gets_its_size_and_two_draws_agree():
    sample = drawn()
    assert sample == drawn()
    assert dict(Counter(r.stratum for r in sample.records)) == SMALL
    assert sample.strata == {
        "provenance-pr": (5, 2),
        "archaeology-pr": (6, 3),
        "provenance-commit": (3, 2),
        "archaeology-commit": (3, 2),
    }


def test_the_draw_takes_the_smallest_hashes_and_orders_by_another():
    sample = drawn()
    smallest = sorted((f"pp{i}" for i in range(5)), key=lambda q: h("gold-validity-v1", q))[:2]
    assert {r.qid for r in sample.records if r.stratum == "provenance-pr"} == set(smallest)
    order = [r.qid for r in sample.records]
    assert order == sorted(order, key=lambda q: h("gold-validity-order-v1", q))
    assert [r.order for r in sample.records] == list(range(1, 10))


def test_the_relabel_subset_comes_from_the_sample_in_its_own_order():
    sample = drawn()
    subset = sample.relabel_records()
    assert len(subset) == 3 and {r.qid for r in subset} <= {r.qid for r in sample.records}
    assert [r.relabel_order for r in subset] == [1, 2, 3]
    by_order = sorted((r.qid for r in subset), key=lambda q: h("gold-validity-relabel-order-v1", q))
    assert [r.qid for r in subset] == by_order


def test_a_stratum_smaller_than_its_size_refuses():
    with pytest.raises(AuditError, match="provenance-commit holds 3"):
        draw_sample(
            golden(),
            pool_version=VERSION,
            corpus_commit=CORPUS,
            allocation={**SMALL, "provenance-commit": 4},
        )


def test_the_sample_round_trips_and_is_never_overwritten(tmp_path):
    path = tmp_path / "audit" / "sample.json"
    sample = drawn()
    write_sample(sample, path)
    assert load_sample(path) == sample
    with pytest.raises(SampleExists):
        write_sample(sample, path)


def test_a_changed_record_or_pool_makes_the_audit_stale():
    records = golden()
    sample = drawn(records)
    assert set(check_fresh(sample, records, VERSION)) == {r.qid for r in sample.records}
    with pytest.raises(AuditStale, match="pool"):
        check_fresh(sample, records, "rationale-v1:commit=9")
    victim = sample.records[0].qid
    changed = [
        r.model_copy(update={"question": "Why was `other` added to `pkg/m.py`?"})
        if r.qid == victim
        else r
        for r in records
    ]
    with pytest.raises(AuditStale, match=victim):
        check_fresh(sample, changed, VERSION)
    with pytest.raises(AuditStale, match=victim):
        check_fresh(sample, [r for r in records if r.qid != victim], VERSION)


def test_the_corpus_commit_is_read_only_from_a_set_measured_on_this_pool():
    assert corpus_commit_of(golden(), VERSION) == CORPUS
    with pytest.raises(AuditError, match="pace golden generate"):
        corpus_commit_of(golden(), "rationale-v1:commit=9")


DAY = date(2026, 11, 1)


def label(qid, stratum, first, final=None, *, on=DAY, comments=False):
    linked = stratum.endswith("-pr")
    return LabelRecord(
        qid, stratum, first, final or first, None, on, False, comments if linked else None
    )


def test_a_label_line_keeps_the_codebooks_rules():
    with pytest.raises(AuditError, match="claims less"):
        LabelRecord("q", "archaeology-pr", "rationale", "mentions", None, DAY, False, False)
    with pytest.raises(AuditError, match="commit-only"):
        LabelRecord("q", "archaeology-commit", "mentions", "rationale", None, DAY, False, None)
    with pytest.raises(AuditError, match="commit-only"):
        LabelRecord("q", "archaeology-commit", "mentions", "mentions", None, DAY, False, False)
    with pytest.raises(AuditError, match="PR-linked"):
        LabelRecord("q", "archaeology-pr", "mentions", "mentions", None, DAY, False, None)
    with pytest.raises(AuditError, match="not one of"):
        LabelRecord("q", "archaeology-pr", "maybe", "maybe", None, DAY, False, False)


def test_labels_round_trip_and_a_double_label_refuses(tmp_path):
    path = tmp_path / "labels.jsonl"
    first = label("a", "archaeology-pr", "mentions", "rationale", comments=True)
    append_label(path, first)
    append_label(path, label("b", "provenance-commit", "unrelated"))
    assert list(load_labels(path)) == ["a", "b"] and load_labels(path)["a"] == first
    append_label(path, first)
    with pytest.raises(AuditError, match="labelled twice"):
        load_labels(path)
    assert load_labels(tmp_path / "missing.jsonl") == {}


def test_the_relabel_waits_for_every_first_label_and_seven_days():
    sample = drawn()
    subset = sample.relabel_records()
    partial = {r.qid: label(r.qid, r.stratum, "mentions") for r in subset[:-1]}
    with pytest.raises(AuditError, match="no first label"):
        check_relabel_allowed(sample, partial, DAY + timedelta(days=30))
    every = {r.qid: label(r.qid, r.stratum, "mentions") for r in subset}
    with pytest.raises(AuditError, match="opens on 2026-11-08"):
        check_relabel_allowed(sample, every, DAY + timedelta(days=6))
    check_relabel_allowed(sample, every, DAY + timedelta(days=7))
