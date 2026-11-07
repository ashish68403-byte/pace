"""Slice B5a's ablation, the pure half: rows, comparisons, the validity, the cross-check."""

from __future__ import annotations

import json

import pytest

from provenance.eval.ablation import (
    AblationCheckFailed,
    build_table,
    compare,
    cross_check,
    hits_at_k,
    per_query_lines,
    read_validity,
    render_json,
)
from provenance.eval.metrics import mde


def row(qid, rank, *, cls="archaeology", df=1):
    recall = 0.0 if rank is None else 1.0
    return {
        "qid": qid,
        "query_class": cls,
        "k": 10,
        "recall_at_k": recall,
        "mrr": 0.0 if rank is None else 1 / rank,
        "ndcg_at_k": recall,
        "citation_precision": 0.0,
        "refused": False,
        "expected_refusal": False,
        "latency_ms": 1.0,
        "unresolved_anchors": 0,
        "first_gold_rank": rank,
        "gold_symbol_df": df,
        "baseline_grep_r10": 0.0,
    }


def rows_for(ranks):
    return [row(f"q{i}", rank) for i, rank in enumerate(ranks)]


REF = rows_for([1, None, 3, None, None, 2])  # hits q0, q2, q5
SYS = rows_for([1, 2, 3, None, None, 4])  # hits q0, q1, q2, q5: differs from REF on q1 only
CURRENT = {"status": "current", "source": "audit.json", "rate": 0.8, "ci95": [0.5, 1.0]}


def test_hits_count_records_with_any_gold_in_the_top_k():
    assert (hits_at_k(REF), hits_at_k(SYS)) == (3, 4)


def test_a_comparison_names_d_the_mde_and_quotes_the_difference_twice():
    c = compare(SYS, REF, CURRENT)
    assert c["delta"] == pytest.approx(1 / 6)
    assert (c["discordant"], c["n"]) == (1, 6)
    assert c["mde"] == pytest.approx(mde(6, 1 / 6))
    assert c["over_validity"]["at_point"] == pytest.approx((1 / 6) / 0.8)
    assert c["over_validity"]["at_upper"] == pytest.approx(1 / 6)
    assert c["over_validity"]["at_lower"] == pytest.approx((1 / 6) / 0.5)


def test_identical_rows_have_no_mde_and_without_validity_nothing_is_divided():
    c = compare(REF, REF, {"status": "missing", "source": "audit.json"})
    assert (c["discordant"], c["mde"]) == (0, None)
    assert "over_validity" not in c


def audit_file(tmp_path, *, status="complete", pool="v1"):
    report = {
        "sample": {"pool_version": pool},
        "labels": {"status": status, "overall": {"rate": 0.955, "ci95": [0.85, 0.99]}},
    }
    path = tmp_path / "audit.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_the_validity_says_why_it_cannot_be_used(tmp_path):
    assert read_validity(tmp_path / "missing.json", "v1")["status"] == "missing"
    assert read_validity(audit_file(tmp_path, status="incomplete"), "v1")["status"] == "incomplete"
    assert read_validity(audit_file(tmp_path, pool="v0"), "v1")["status"] == "stale"
    current = read_validity(audit_file(tmp_path), "v1")
    assert (current["status"], current["rate"], current["ci95"]) == ("current", 0.955, [0.85, 0.99])


def verdict_file(tmp_path, *, grep=3, ident=3, n=6, pool=100):
    verdict = {
        "n_retained": n,
        "pool_size": pool,
        "baselines": [
            {"name": "grep", "hits_at_10": grep},
            {"name": "bm25_ident", "hits_at_10": ident},
        ],
    }
    path = tmp_path / "verdict.json"
    path.write_text(json.dumps(verdict), encoding="utf-8")
    return path


def test_the_cross_check_passes_only_when_the_two_computations_agree(tmp_path):
    hits = {"grep": 3, "bm25_ident": 3}
    assert cross_check(verdict_file(tmp_path), hits, records=6, pool_size=100)["grep"] == 3
    with pytest.raises(AblationCheckFailed, match="disagree"):
        cross_check(verdict_file(tmp_path, ident=4), hits, records=6, pool_size=100)
    with pytest.raises(AblationCheckFailed, match="regenerate the verdict"):
        cross_check(verdict_file(tmp_path), hits, records=7, pool_size=100)
    with pytest.raises(AblationCheckFailed, match="does not exist"):
        cross_check(tmp_path / "none.json", hits, records=6, pool_size=100)


META = {"corpus_commit": "c" * 40, "pool_version": "v1", "golden_path": "g.jsonl", "records": 6}
CHECKED = {"source": "verdict.json", "grep": 4, "bm25_ident": 3}


def test_the_table_is_the_same_bytes_twice_and_keeps_no_timings():
    rows = {"grep": SYS, "bm25_ident": REF}
    first = render_json(build_table(rows, meta=META, validity=CURRENT, checked=CHECKED))
    second = render_json(build_table(rows, meta=META, validity=CURRENT, checked=CHECKED))
    assert first == second
    table = json.loads(first)
    assert "latency" not in first and table["reference"] == "bm25_ident"
    assert table["comparisons"]["grep"]["discordant"] == 1
    assert table["rows"]["grep"]["hits@10"] == 4 and "bm25_ident" not in table["comparisons"]


def test_the_per_query_file_has_one_sorted_line_per_record():
    parsed = [json.loads(line) for line in per_query_lines({"grep": SYS, "bm25_ident": REF})]
    assert [p["qid"] for p in parsed] == [f"q{i}" for i in range(6)]
    assert parsed[1]["first_gold_rank"] == {"bm25_ident": None, "grep": 2}
