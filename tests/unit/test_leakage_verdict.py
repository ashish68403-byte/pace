"""The leakage probe: one record's facts (ADR-0003 §5), and checkpoint B's verdict (§6)."""

from __future__ import annotations

import numpy as np

from provenance.eval.leakage import Document, PoolIndex, rank

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
