"""The leakage probe: one record's facts (ADR-0003 §5), and checkpoint B's verdict (§6)."""

from __future__ import annotations

import numpy as np
import pytest

from provenance.eval.leakage import (
    FAIL,
    INCONCLUSIVE,
    PASS,
    Document,
    Gate,
    PoolIndex,
    ResolvedGold,
    build_pool_index,
    rank,
    render_report,
    resolve_gold_from_pool,
    run_probe,
    verdict,
)
from provenance.eval.metrics import wilson_interval
from provenance.eval.schema import GoldenRecord

POOL = [
    Document("g", "Fix the scheduler loop: call _find_zombies once", "commit/g"),
    Document("o1", "Bump version", "commit/o1"),
    Document("o2", "Refactor heartbeat handling in the scheduler", "commit/o2"),
]


def test_a_question_naming_its_gold_identifier_leaks():
    question = "Why was `_find_zombies` added to `scheduler.py`?"
    facts = PoolIndex(POOL).measure(question, {"g"}, "_find_zombies")
    assert facts.grep_answers_at_rank_1
    assert "_find_zombies" in facts.rare_tokens_shared_with_gold
    assert (facts.grep_hit_at_k, facts.bm25_hit_at_k, facts.gold_symbol_df) == (True, True, 1)
    assert not facts.retained()


def test_a_question_whose_words_point_elsewhere_does_not_leak():
    question = "Why did `heartbeat` change in March 2023?"
    facts = PoolIndex(POOL).measure(question, {"g"}, "heartbeat")
    assert (facts.grep_answers_at_rank_1, facts.rare_tokens_shared_with_gold) == (False, [])
    assert (facts.grep_hit_at_k, facts.bm25_hit_at_k, facts.gold_symbol_df) == (False, False, 1)
    assert facts.retained()


def test_a_document_that_matched_nothing_is_never_retrieved():
    # Nothing in POOL contains `retry_policy` or "change": grep matches no document. `rank`
    # still lists index 0 -- the gold document -- first; reading that as "rank 1" would
    # drop a record grep cannot answer at all.
    assert rank(np.zeros(3), 1) == [0]
    question = "Why did `retry_policy` change in May 2021?"
    facts = PoolIndex(POOL).measure(question, {"g"}, "retry_policy")
    assert (facts.grep_answers_at_rank_1, facts.grep_hit_at_k) == (False, False)
    assert facts.gold_symbol_df == 0


def test_ties_break_on_the_lower_index_at_every_k():
    scores = np.array([0.0, 0.0, 1.0, 1.0])
    assert rank(scores, 1) == [2]
    assert rank(scores, 4) == [2, 3, 0, 1]


def gate(**overrides):
    fields = {
        "n_candidates": 200,
        "n_retained": 150,
        "stale": 0,
        "strongest": "bm25_ident",
        "strongest_upper": 0.2,
    }
    fields.update(overrides)
    return Gate(**fields)


def test_a_clean_benchmark_of_enough_records_passes():
    assert verdict(gate()) == (PASS, [])


def test_fewer_than_100_retained_dev_records_is_inconclusive_never_pass():
    assert verdict(gate(n_retained=99))[0] == INCONCLUSIVE
    assert verdict(gate(n_retained=100))[0] == PASS


def test_retention_below_a_quarter_fails():
    assert verdict(gate(n_candidates=1000, n_retained=249))[0] == FAIL  # 24.9%
    assert verdict(gate(n_candidates=1000, n_retained=250))[0] == PASS


def test_an_upper_bound_of_exactly_the_ceiling_fails():
    assert verdict(gate(strongest_upper=0.35))[0] == FAIL
    assert verdict(gate(strongest_upper=0.3499))[0] == PASS


def test_a_stale_fact_is_inconclusive_even_when_all_else_would_pass():
    outcome, reasons = verdict(gate(stale=1))
    assert outcome == INCONCLUSIVE and "pace golden generate" in reasons[0]


def sha(i):
    return f"{i:040x}"


def generated(qid, question, gold_sha, *, split="dev", rank_1=False, rare=()):
    return GoldenRecord.model_validate(
        {
            "qid": qid,
            "query_class": "archaeology",
            "split": split,
            "question": question,
            "required_evidence": {"commit_shas": [gold_sha]},
            "supporting_evidence": {
                "symbol_anchors": [{"path": "pkg/x.py", "qualified_name": "x"}]
            },
            "generation_strategy": "ast_diff_v0",
            "leakage": {
                "baseline_grep_r10": 0.0,
                "baseline_bm25_r10": 0.0,
                "gold_symbol_df": 0,
                "grep_answers_at_rank_1": rank_1,
                "rare_tokens_shared_with_gold": list(rare),
            },
        }
    )


def probe(records, pool):
    index = build_pool_index(pool)
    return run_probe(records, pool, lambda record: resolve_gold_from_pool(record, index))


def tidy_pool(n):
    """n documents that share no word with any `fn_<i>` question."""
    return [
        Document(
            f"d{i}", "Tidy the loader", f"commit/{sha(i)}", evidence=frozenset({("commit", sha(i))})
        )
        for i in range(n)
    ]


def fn_question(i):
    return f"Why did `fn_{i}` change in March 2023?"


def test_a_benchmark_no_lexical_baseline_can_answer_passes():
    records = [generated(f"q{i}", fn_question(i), sha(i)) for i in range(150)]
    report = probe(records, tidy_pool(150))
    assert (report["verdict"], report["n_retained"], report["n_stale"]) == (PASS, 150, 0)
    assert report["strongest_cheap_upper_95"] == pytest.approx(wilson_interval(0, 150)[1])
    assert "VERDICT: PASS" in render_report(report)


def test_only_the_dev_split_is_read():
    dev = generated("q-dev", fn_question(0), sha(0))
    # A test record with a wrong stored fact: reading it at all would make the run stale.
    test = generated("q-test", fn_question(1), sha(1), split="test", rank_1=True)
    report = probe([dev, test], tidy_pool(2))
    assert (report["n_golden"], report["n_candidates"], report["n_stale"]) == (2, 1, 0)


def test_a_stored_fact_the_pool_contradicts_makes_the_run_inconclusive():
    record = generated("q0", fn_question(0), sha(0), rank_1=True)
    report = probe([record], tidy_pool(1))
    assert (report["verdict"], report["n_stale"], report["stale"]) == (INCONCLUSIVE, 1, ["q0"])
    assert "pace golden generate" in report["verdict_reasons"][0]


def test_the_gate_reads_the_strongest_lexical_baseline_not_grep():
    # "into" is a grep stopword but a BM25 token, in 7 documents (df 7 > 5: not rare).
    # BM25 reaches the gold document; grep matches nothing.
    pool = [Document("g", "Moved into the cache", evidence=frozenset({("commit", sha(0))}))]
    pool += [Document(f"o{i}", "Fell into the sea") for i in range(6)]
    report = probe([generated("q0", "Why did `qq_rr` go into?", sha(0))], pool)
    hits = {b["name"]: b["hits_at_10"] for b in report["baselines"] if b["measured"]}
    assert hits == {"grep": 0, "bm25_default": 1, "bm25_ident": 1}
    assert report["strongest_cheap_baseline"] == "bm25_default"
    assert report["strongest_cheap_upper_95"] == pytest.approx(wilson_interval(1, 1)[1])


def test_an_empty_run_is_inconclusive_and_still_renders():
    report = run_probe([], [], lambda record: ResolvedGold(qid=record.qid))
    assert (report["verdict"], report["n_candidates"]) == (INCONCLUSIVE, 0)
    assert "VERDICT: INCONCLUSIVE" in render_report(report)
