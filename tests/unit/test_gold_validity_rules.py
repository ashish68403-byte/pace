"""ADR-0004's audit, the pure half: the sample, staleness, label files and the report."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

from provenance.eval.gold_validity import (
    AuditError,
    AuditStale,
    LabelRecord,
    SampleExists,
    agreement_section,
    append_label,
    build_report,
    check_fresh,
    check_relabel_allowed,
    corpus_commit_of,
    draw_sample,
    label_section,
    load_labels,
    load_sample,
    population,
    reachability_section,
    render_report_json,
    render_report_summary,
    stratum_of,
    write_sample,
)
from provenance.eval.metrics import StratumCount, stratified_proportion
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


def labelled(sample, rule):
    """A label for every sampled record: `rule(stratum, i)` gives (first, final) for the
    i-th sampled record of its stratum."""
    seen: Counter[str] = Counter()
    labels = {}
    for r in sample.records:
        first, final = rule(r.stratum, seen[r.stratum])
        seen[r.stratum] += 1
        labels[r.qid] = label(r.qid, r.stratum, first, final)
    return labels


def test_an_incomplete_labelling_gives_no_rate():
    sample = drawn()
    labels = labelled(sample, lambda s, i: ("rationale", "rationale"))
    del labels[sample.records[-1].qid]
    assert label_section(sample, labels) == {"status": "incomplete", "labelled": 8, "of": 9}


def test_a_complete_labelling_gives_every_rate():
    sample = drawn()

    def rule(stratum, i):
        # PR-linked: the first record of a stratum gives the reason in its commit message,
        # the rest only once the PR is read. Commit-only: one reason, the rest unrelated.
        if stratum.endswith("-pr"):
            return ("rationale", "rationale") if i == 0 else ("mentions", "rationale")
        return ("rationale", "rationale") if i == 0 else ("unrelated", "unrelated")

    section = label_section(sample, labelled(sample, rule))
    assert section["status"] == "complete"
    archaeology = section["by_stratum"]["archaeology-pr"]
    assert archaeology["labels"] == {"rationale": 3, "mentions": 0, "unrelated": 0}
    assert archaeology["message_only"]["labels"] == {"rationale": 1, "mentions": 2, "unrelated": 0}
    expected = stratified_proportion(
        [
            StratumCount(3, 2, 1),  # archaeology-commit
            StratumCount(6, 3, 3),  # archaeology-pr
            StratumCount(3, 2, 1),  # provenance-commit
            StratumCount(5, 2, 2),  # provenance-pr
        ]
    )
    assert section["overall"]["rate"] == round(expected.estimate, 4)
    assert section["pr_text"]["supplied_by_pr_text"] == 3  # two archaeology, one provenance
    assert section["pr_text"]["with_pr_text"]["rate"] == 1.0
    assert section["reading"]["pr_text_shown"] == 3


def test_a_label_from_another_stratum_refuses():
    sample = drawn()
    labels = {r.qid: label(r.qid, r.stratum, "mentions") for r in sample.records}
    commit_only = next(r for r in sample.records if r.stratum.endswith("-commit"))
    labels[commit_only.qid] = label(commit_only.qid, "archaeology-pr", "mentions")
    with pytest.raises(AuditError, match="sampled as"):
        label_section(sample, labels)


def test_self_agreement_waits_for_every_relabel_then_reports_kappa():
    sample = drawn()
    subset = sample.relabel_records()
    labels = {r.qid: label(r.qid, r.stratum, "mentions") for r in sample.records}
    for r, value in zip(subset, ("rationale", "mentions", "unrelated"), strict=True):
        labels[r.qid] = label(r.qid, r.stratum, value)
    assert agreement_section(sample, labels, {})["status"] == "not_measured"
    relabels = {
        r.qid: label(r.qid, r.stratum, value)
        for r, value in zip(subset, ("rationale", "mentions", "mentions"), strict=True)
    }
    measured = agreement_section(sample, labels, relabels)
    assert (measured["status"], measured["n"]) == ("measured", 3)
    # observed 2/3; chance 1/9 + 2/9 + 0 = 1/3; kappa (2/3 - 1/3) / (2/3) = 0.5
    assert measured["final"] == {"observed": 0.6667, "kappa": 0.5}


def test_a_unanimous_relabel_reports_kappa_as_undefined():
    sample = drawn()
    labels = {r.qid: label(r.qid, r.stratum, "mentions") for r in sample.records}
    relabels = {r.qid: labels[r.qid] for r in sample.relabel_records()}
    final = agreement_section(sample, labels, relabels)["final"]
    assert final["kappa"] is None and final["observed"] == 1.0 and "kappa_undefined" in final


def test_reachability_counts_unreachable_records_per_stratum():
    sample = drawn()
    reached = {"pp0": True, "pp1": False, "ap0": False}
    strata = {"pp0": "provenance-pr", "pp1": "provenance-pr", "ap0": "archaeology-pr"}
    section = reachability_section(sample, reached, strata)
    assert (section["records"], section["unreachable"], section["share"]) == (3, 2, 0.6667)
    assert section["by_stratum"]["provenance-pr"] == {"records": 2, "unreachable": 1, "share": 0.5}
    empty = {"records": 0, "unreachable": 0, "share": None}
    assert section["by_stratum"]["provenance-commit"] == empty
    assert section["k"] == 100 and "not measured" in section["dense"]


def test_the_report_is_the_same_bytes_twice_and_summarises():
    sample = drawn()
    labels = {r.qid: label(r.qid, r.stratum, "mentions") for r in sample.records}
    reach = reachability_section(sample, {"pp0": True}, {"pp0": "provenance-pr"})
    bias = {"groups": {"linked": 3, "no_reference": 4}, "module_tvd": 0.1, "year_tvd": 0.2}

    def render():
        report = build_report(sample, labels, {}, reachability=reach, linkage_bias=bias)
        return report, render_report_json(report)

    (report, first), (_, second) = render(), render()
    assert first == second and json.loads(first)["labels"]["status"] == "complete"
    summary = render_report_summary(report, Path("out.json"))
    assert "validity 0.0%" in summary and "not measured (0 of 3 re-labelled)" in summary
