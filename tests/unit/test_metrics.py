"""Metric semantics, including the two that are easy to report dishonestly.

Three properties get most of the attention here because each of them is a number
that would look fine in a dissertation while being wrong:

* **NaN is not zero.** A query with no required evidence is *undefined*, not
  *failed*. Coerce it to 0.0 and every unanswerable in the set drags headline
  recall down by its share of the set, which reads as a retrieval regression and
  is actually a bookkeeping error.
* **Refusal precision and recall are only meaningful as a pair.** Refuse
  everything and recall is 1.00. Refuse one obvious negative and precision is
  1.00. The test below asserts the degenerate strategies explicitly, because
  "our refusal recall is 1.0" is exactly the sentence a viva will stop on.
* **The bootstrap must be stratified.** Unanswerables are ~20% of the set; an
  unstratified resample sometimes draws zero of them, refusal recall becomes 0/0
  in that resample, and the interval is silently wrong.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from provenance.eval.metrics import (
    BOOTSTRAP_SEED,
    CI,
    bootstrap_ci,
    citation_precision,
    format_metric,
    mrr,
    ndcg_at_k,
    paired_bootstrap,
    percentile,
    recall_at_k,
    refusal_precision,
    refusal_recall,
    resample_indices,
)

# --------------------------------------------------------------------------- recall


def test_recall_at_k_is_the_fraction_of_required_evidence_found() -> None:
    retrieved = ["c1", "c2", "c3", "c4"]
    assert recall_at_k(retrieved, {"c1", "c3"}, k=4) == 1.0
    assert recall_at_k(retrieved, {"c1", "zzz"}, k=4) == 0.5
    assert recall_at_k(retrieved, {"zzz"}, k=4) == 0.0


def test_recall_at_k_respects_the_cut_off() -> None:
    retrieved = ["c1", "c2", "c3", "c4"]
    assert recall_at_k(retrieved, {"c4"}, k=3) == 0.0
    assert recall_at_k(retrieved, {"c4"}, k=4) == 1.0


def test_recall_is_nan_when_there_is_no_required_evidence() -> None:
    """Unanswerables are undefined for recall, not zero.

    If this ever returns 0.0, headline recall silently becomes
    (answerables' recall) x (fraction answerable), and every reported number in
    the write-up is scaled by a constant nobody wrote down.
    """
    assert math.isnan(recall_at_k(["c1", "c2"], set(), k=10))
    assert math.isnan(recall_at_k([], set(), k=10))


def test_recall_ignores_supporting_evidence() -> None:
    """Only the required tier counts, so easy corroboration cannot inflate it."""
    assert recall_at_k(["supporting-1", "supporting-2"], {"required-1"}, k=10) == 0.0


# ------------------------------------------------------------------------------ mrr


def test_mrr_is_the_reciprocal_of_the_first_relevant_rank() -> None:
    assert mrr(["a", "b", "c"], {"a"}) == 1.0
    assert mrr(["a", "b", "c"], {"b"}) == 0.5
    assert mrr(["a", "b", "c"], {"c"}) == pytest.approx(1 / 3)
    assert mrr(["a", "b", "c"], {"b", "c"}) == 0.5  # first hit wins


def test_mrr_is_zero_inside_the_horizon_and_nan_without_relevance() -> None:
    assert mrr(["a", "b", "c"], {"z"}) == 0.0
    assert mrr(["a", "b", "c"], {"c"}, k=2) == 0.0  # relevant, but past the cut-off
    assert math.isnan(mrr(["a"], set()))


# ----------------------------------------------------------------------------- ndcg


def test_ndcg_prefers_required_evidence_over_supporting() -> None:
    gains = {"req": 2.0, "sup": 1.0}
    required_first = ndcg_at_k(["req", "sup"], gains, k=10)
    supporting_first = ndcg_at_k(["sup", "req"], gains, k=10)
    assert required_first == 1.0
    assert supporting_first < required_first


def test_ndcg_is_nan_without_graded_evidence() -> None:
    assert math.isnan(ndcg_at_k(["a"], {}, k=10))


def test_citation_precision_is_nan_when_nothing_was_cited() -> None:
    """An answer with no citations is a refusal or a bug; it is measured by the
    refusal metrics, and scoring it 0.0 here would double-count it."""
    assert math.isnan(citation_precision([], {"commit:abc"}))
    assert citation_precision(["commit:abc", "commit:zzz"], {"commit:abc"}) == 0.5


# -------------------------------------------------------------------------- refusal


def test_refusing_everything_scores_recall_one_and_poor_precision() -> None:
    """THE reason the pair is always reported together.

    A system that refuses every question catches every unanswerable -- recall
    1.00, the best possible score -- while being useless, and its uselessness
    shows up only in precision, which collapses to the base rate of
    unanswerables in the set.
    """
    is_unanswerable = [False] * 8 + [True] * 2
    refuse_everything = [True] * 10

    assert refusal_recall(refuse_everything, is_unanswerable) == 1.0
    assert refusal_precision(refuse_everything, is_unanswerable) == pytest.approx(0.2)


def test_refusing_one_obvious_negative_scores_precision_one_and_poor_recall() -> None:
    """The mirror-image gaming strategy, which precision alone would reward."""
    is_unanswerable = [False] * 8 + [True] * 2
    refuse_one = [False] * 9 + [True]

    assert refusal_precision(refuse_one, is_unanswerable) == 1.0
    assert refusal_recall(refuse_one, is_unanswerable) == 0.5


def test_refusal_precision_is_nan_when_nothing_was_refused() -> None:
    """0/0, not 1.0: a system that never refuses has no precision to report."""
    assert math.isnan(refusal_precision([False, False], [True, False]))


def test_refusal_recall_is_nan_without_any_unanswerable() -> None:
    assert math.isnan(refusal_recall([True, False], [False, False]))


def test_refusal_metrics_reject_misaligned_arms() -> None:
    with pytest.raises(ValueError):
        refusal_recall([True], [True, False])


# ------------------------------------------------------------------------ bootstrap


def test_stratified_resample_keeps_every_stratum_at_its_real_size() -> None:
    """The property that makes an interval on refusal recall meaningful at all.

    With 2 unanswerables in 10 queries, an UNstratified bootstrap draws zero of
    them in (8/10)^10 ~= 11% of resamples. In those resamples refusal recall is
    0/0 and the interval is quietly computed over a different quantity. Stratified
    resampling makes that impossible, and this asserts it directly rather than
    trusting the docstring.
    """
    strata = np.array([0] * 8 + [1] * 2)
    idx = resample_indices(strata, b=500, seed=BOOTSTRAP_SEED)

    assert idx.shape == (500, 10)
    drawn = strata[idx]
    assert (drawn == 1).sum(axis=1).min() == 2
    assert (drawn == 1).sum(axis=1).max() == 2
    assert (drawn == 0).sum(axis=1).tolist() == [8] * 500


def test_bootstrap_ci_brackets_the_point_estimate_and_is_reproducible() -> None:
    # Variance WITHIN each stratum, or the stratified resample would be a
    # constant and the interval would be trivially zero-width.
    values = [1.0] * 20 + [0.0] * 10 + [1.0] * 5 + [0.0] * 5
    strata = ["provenance"] * 30 + ["unanswerable"] * 10

    first = bootstrap_ci(values, strata, b=2000)
    second = bootstrap_ci(values, strata, b=2000)

    assert first == second  # committed seed -> byte-identical intervals
    assert first.n == 40
    assert first.point == pytest.approx(0.625)
    assert first.lo <= first.point <= first.hi
    assert first.hi > first.lo
    assert first.lo >= 0.0
    assert first.hi <= 1.0


def test_bootstrap_ci_on_a_constant_has_zero_width() -> None:
    ci = bootstrap_ci([0.7] * 20, b=200)
    assert ci.point == pytest.approx(0.7)
    assert ci.lo == pytest.approx(0.7)
    assert ci.hi == pytest.approx(0.7)


def test_bootstrap_drops_nan_queries_instead_of_scoring_them_zero() -> None:
    """n reports how many queries were actually scorable, and the mean is over
    those. A NaN coerced to 0.0 here would look like a real, reportable drop."""
    ci = bootstrap_ci([1.0, float("nan"), 1.0], b=200)
    assert ci.n == 2
    assert ci.point == pytest.approx(1.0)


def test_empty_and_all_nan_inputs_give_clean_zeros() -> None:
    """Week 1 runs on an empty golden set and the gate must still print a table
    and exit 0 -- so this returns a zero CI, not NaN and not an exception."""
    assert bootstrap_ci([]) == CI(0.0, 0.0, 0.0, 0)
    assert bootstrap_ci([float("nan"), float("nan")]) == CI(0.0, 0.0, 0.0, 0)

    paired = paired_bootstrap([], [])
    assert paired.n == 0
    assert paired.delta == 0.0
    assert paired.significant is False


def test_paired_bootstrap_detects_a_uniform_improvement() -> None:
    """Same queries, same resample vector: common difficulty cancels."""
    baseline = [0.0] * 20 + [1.0] * 20
    system = [1.0] * 20 + [1.0] * 20
    strata = ["provenance"] * 20 + ["archaeology"] * 20

    paired = paired_bootstrap(system, baseline, strata, b=2000)

    assert paired.n == 40
    assert paired.delta == pytest.approx(0.5)
    assert paired.lo > 0.0
    assert paired.significant is True
    assert paired.prob_not_better < 0.05


def test_paired_bootstrap_reports_no_difference_for_identical_arms() -> None:
    values = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0]
    paired = paired_bootstrap(values, values, b=500)
    assert paired.delta == 0.0
    assert paired.significant is False
    assert paired.prob_not_better == 1.0  # every resample has delta <= 0


def test_paired_bootstrap_drops_a_query_undefined_in_either_arm() -> None:
    paired = paired_bootstrap([1.0, float("nan"), 1.0], [0.0, 0.0, float("nan")], b=200)
    assert paired.n == 1


def test_paired_bootstrap_rejects_misaligned_arms() -> None:
    with pytest.raises(ValueError):
        paired_bootstrap([1.0, 0.0], [1.0])


# -------------------------------------------------------------------------- render


def test_format_metric_always_carries_the_interval_and_the_n() -> None:
    assert format_metric(0.812, 0.74, 0.87, 150) == "0.81 [0.74, 0.87], n=150"
    assert format_metric(0.05, -0.01, 0.11, 40, signed=True) == "+0.05 [-0.01, 0.11], n=40"
    assert format_metric(float("nan"), 0.0, 0.0, 0) == "n/a, n=0"


def test_percentile_ignores_undefined_values() -> None:
    assert percentile([10.0, float("nan"), 20.0], 50) == pytest.approx(15.0)
    assert math.isnan(percentile([], 95))
