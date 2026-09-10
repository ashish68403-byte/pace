"""Retrieval, citation and refusal metrics, plus a stratified bootstrap.

What this module is for: turning a per-query table into numbers that can be
written in a dissertation without an examiner being able to knock them over.

The failures it prevents:

* Reporting a point estimate with no interval. On n=150 a recall@10 of 0.81 has a
  95% interval roughly +/-0.06; two systems 3 points apart are indistinguishable
  and saying otherwise is a false claim.
* An unstratified bootstrap. Unanswerables are ~20% of the set; an unstratified
  resample sometimes draws zero of them, refusal recall becomes 0/0, and the
  interval on the refusal metrics is silently wrong. Resampling *within* class
  fixes it and keeps class proportions identical to the real set.
* Comparing two overlapping marginal CIs and concluding "no significant
  difference". That is the classic error: the variance of a *difference* between
  two systems scored on the SAME queries is far smaller than the sum of the two
  marginal variances, because query difficulty is common noise that cancels.
  ``paired_bootstrap`` resamples one index vector and applies it to both systems,
  which typically yields an interval 3-5x tighter than eyeballing overlap.

Pure numpy on purpose: scipy is ~90 MB of wheels for a percentile and an argsort,
and this runs on a 2-core box with 3.7 GB of RAM.

Missing-value convention: a metric that is undefined for a query (recall with no
required evidence, nDCG with no graded evidence, citation precision with no
citations) returns ``float("nan")`` and is dropped -- with its stratum label --
before aggregation. It is never coerced to 0.0; a query that cannot be scored is
not a query that scored zero.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

#: Number of bootstrap resamples. 10_000 puts Monte-Carlo error on a 95% percentile
#: interval well under half a recall point, which is below anything we would claim.
BOOTSTRAP_B = 10_000

#: Committed, fixed seed. It lives in the repo so that a number in the write-up can
#: be reproduced byte-for-byte from the per-query parquet. Changing it silently
#: makes every previously published interval non-reproducible -- do not.
BOOTSTRAP_SEED = 20250131

#: Graded relevance gains for nDCG. Required evidence is worth strictly more than
#: supporting, so surfacing corroboration cannot compensate for missing the commit
#: that actually answers the question.
REQUIRED_GAIN = 2.0
SUPPORTING_GAIN = 1.0

DEFAULT_K = 10


# --------------------------------------------------------------------------- #
# Per-query metrics
# --------------------------------------------------------------------------- #
def recall_at_k(retrieved: Sequence[str], required: Collection[str], k: int = DEFAULT_K) -> float:
    """Fraction of REQUIRED evidence found in the top k.

    Headline recall uses the required tier only. Supporting evidence entering
    recall would let a system inflate the number by retrieving easy corroboration
    while missing the decisive commit.

    Returns NaN when the query has no required evidence (i.e. unanswerables) --
    undefined, not zero.
    """
    required = set(required)
    if not required:
        return float("nan")
    top = set(retrieved[:k])
    return len(top & required) / len(required)


def mrr(retrieved: Sequence[str], relevant: Collection[str], k: int | None = None) -> float:
    """Reciprocal rank of the first relevant chunk (0.0 if none in the cut-off)."""
    relevant = set(relevant)
    if not relevant:
        return float("nan")
    horizon = retrieved if k is None else retrieved[:k]
    for i, cid in enumerate(horizon):
        if cid in relevant:
            return 1.0 / (i + 1)
    return 0.0


def _dcg(gains: Iterable[float]) -> float:
    return float(sum(g / math.log2(i + 2) for i, g in enumerate(gains)))


def ndcg_at_k(retrieved: Sequence[str], gains: Mapping[str, float], k: int = DEFAULT_K) -> float:
    """nDCG@k with graded relevance (``gains`` from ``ResolvedAnchors.gains``).

    The ideal ranking is the k highest gains available, so a query whose gold has
    fewer than k items can still reach 1.0.
    """
    if not gains:
        return float("nan")
    actual = _dcg(gains.get(cid, 0.0) for cid in retrieved[:k])
    ideal = _dcg(sorted(gains.values(), reverse=True)[:k])
    return actual / ideal if ideal > 0 else float("nan")


def citation_precision(cited: Sequence[str], supported: Collection[str]) -> float:
    """Fraction of emitted citations that are actually in the gold evidence.

    Operates on stable evidence keys (``("commit", sha)`` rendered as strings) or
    on chunk ids -- whatever the caller passes -- as long as both sides agree.
    Returns NaN when nothing was cited: an answer with no citations is a refusal
    or a bug, and is measured by the refusal metrics, not by this one.
    """
    if not cited:
        return float("nan")
    supported = set(supported)
    return sum(1 for c in cited if c in supported) / len(cited)


def refusal_precision(predicted_refusal: Sequence[bool], is_unanswerable: Sequence[bool]) -> float:
    """Of the queries the system refused, how many were genuinely unanswerable.

    GAMEABLE ON ITS OWN: a system that refuses only the single most obvious
    unanswerable scores 1.00 here. So does one that refuses nothing (0/0 -> NaN,
    which a careless aggregator would drop). Precision is only meaningful reported
    beside ``refusal_recall``; the write-up quotes both or neither.
    """
    if len(predicted_refusal) != len(is_unanswerable):
        raise ValueError("predicted_refusal and is_unanswerable must be the same length")
    refused = [t for p, t in zip(predicted_refusal, is_unanswerable, strict=True) if p]
    if not refused:
        return float("nan")
    return sum(1 for t in refused if t) / len(refused)


def refusal_recall(predicted_refusal: Sequence[bool], is_unanswerable: Sequence[bool]) -> float:
    """Of the genuinely unanswerable queries, how many the system refused.

    GAMEABLE ON ITS OWN in the opposite direction: refuse everything and this is
    1.00 while precision collapses. The pair is the metric; either alone is a
    number a reviewer will (correctly) ignore.
    """
    if len(predicted_refusal) != len(is_unanswerable):
        raise ValueError("predicted_refusal and is_unanswerable must be the same length")
    truths = [p for p, t in zip(predicted_refusal, is_unanswerable, strict=True) if t]
    if not truths:
        return float("nan")
    return sum(1 for p in truths if p) / len(truths)


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CI:
    """A point estimate with a percentile interval and the n behind it."""

    point: float
    lo: float
    hi: float
    n: int

    def __str__(self) -> str:
        return format_metric(self.point, self.lo, self.hi, self.n)

    def as_dict(self) -> dict[str, float | int]:
        return {"point": self.point, "lo": self.lo, "hi": self.hi, "n": self.n}


@dataclass(frozen=True, slots=True)
class PairedCI:
    """Interval on the DIFFERENCE (system - baseline) over the same queries."""

    delta: float
    lo: float
    hi: float
    n: int
    #: Fraction of resamples where the system was no better than the baseline.
    #: A one-sided bootstrap p-value; use it, not CI overlap.
    prob_not_better: float

    @property
    def significant(self) -> bool:
        """True when the interval excludes 0 in either direction."""
        return self.lo > 0.0 or self.hi < 0.0

    def __str__(self) -> str:
        return f"{format_metric(self.delta, self.lo, self.hi, self.n, signed=True)}"

    def as_dict(self) -> dict[str, float | int | bool]:
        return {
            "delta": self.delta,
            "lo": self.lo,
            "hi": self.hi,
            "n": self.n,
            "prob_not_better": self.prob_not_better,
            "significant": self.significant,
        }


def _clean(values: Sequence[float], strata: Sequence[str] | None) -> tuple[np.ndarray, np.ndarray]:
    """Drop NaN (undefined) queries, carrying their stratum labels with them."""
    v = np.asarray(values, dtype=float)
    if strata is None:
        s = np.zeros(v.shape[0], dtype=np.int64)
    else:
        if len(strata) != v.shape[0]:
            raise ValueError("values and strata must be the same length")
        _, s = np.unique(np.asarray(strata, dtype=object).astype(str), return_inverse=True)
    keep = ~np.isnan(v)
    return v[keep], s[keep]


def resample_indices(
    strata: np.ndarray, *, b: int = BOOTSTRAP_B, seed: int = BOOTSTRAP_SEED
) -> np.ndarray:
    """(b, n) index matrix for a STRATIFIED bootstrap.

    Each stratum is resampled with replacement to its own original size, so every
    resample has exactly the class composition of the real golden set. That is the
    property that guarantees no resample contains zero unanswerables -- which is
    what makes an interval on refusal recall meaningful at all.

    Exposed (rather than hidden inside ``bootstrap_ci``) precisely so that
    ``paired_bootstrap`` can apply the SAME matrix to both systems.
    """
    rng = np.random.default_rng(seed)
    n = strata.shape[0]
    if n == 0:
        return np.empty((b, 0), dtype=np.int64)
    blocks: list[np.ndarray] = []
    for label in np.unique(strata):
        member = np.flatnonzero(strata == label)
        picks = rng.integers(0, member.shape[0], size=(b, member.shape[0]))
        blocks.append(member[picks])
    return np.concatenate(blocks, axis=1)


def bootstrap_ci(
    values: Sequence[float],
    strata: Sequence[str] | None = None,
    *,
    b: int = BOOTSTRAP_B,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = 0.05,
) -> CI:
    """Stratified percentile bootstrap on the mean of ``values``.

    ``strata`` is normally the query_class column. An empty (or all-NaN) input
    returns a clean zero rather than NaN, because week 1 runs on an empty golden
    set and the CI gate must still print a table and exit 0.
    """
    v, s = _clean(values, strata)
    n = int(v.shape[0])
    if n == 0:
        return CI(0.0, 0.0, 0.0, 0)
    point = float(v.mean())
    idx = resample_indices(s, b=b, seed=seed)
    dist = v[idx].mean(axis=1)
    lo, hi = np.percentile(dist, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return CI(point, float(lo), float(hi), n)


def paired_bootstrap(
    system: Sequence[float],
    baseline: Sequence[float],
    strata: Sequence[str] | None = None,
    *,
    b: int = BOOTSTRAP_B,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = 0.05,
) -> PairedCI:
    """Interval on ``mean(system) - mean(baseline)`` over the same queries.

    Both arms are indexed by the SAME resampled index vector. Query difficulty is
    then common to both arms in every resample and cancels out of the difference,
    which is why this interval is typically 3-5x tighter than the (wrong) test of
    "do the two marginal CIs overlap?".

    Requires the two arms to be aligned query-for-query; the caller must join on
    qid before calling, and a query undefined in either arm is dropped from both.
    """
    a = np.asarray(system, dtype=float)
    c = np.asarray(baseline, dtype=float)
    if a.shape != c.shape:
        raise ValueError("paired arms must be aligned query-for-query")
    if strata is not None and len(strata) != a.shape[0]:
        raise ValueError("values and strata must be the same length")
    keep = ~(np.isnan(a) | np.isnan(c))
    a, c = a[keep], c[keep]
    if strata is None:
        s = np.zeros(a.shape[0], dtype=np.int64)
    else:
        labels = np.asarray(strata, dtype=object).astype(str)[keep]
        _, s = np.unique(labels, return_inverse=True)
    n = int(a.shape[0])
    if n == 0:
        return PairedCI(0.0, 0.0, 0.0, 0, 1.0)
    delta = float(a.mean() - c.mean())
    idx = resample_indices(s, b=b, seed=seed)
    dist = a[idx].mean(axis=1) - c[idx].mean(axis=1)
    lo, hi = np.percentile(dist, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    prob_not_better = float(np.mean(dist <= 0.0))
    return PairedCI(delta, float(lo), float(hi), n, prob_not_better)


def format_metric(
    point: float, lo: float, hi: float, n: int, digits: int = 2, *, signed: bool = False
) -> str:
    """Render as ``0.81 [0.74, 0.87], n=150``.

    One rendering function, used by the runner, the README tables and the write-up,
    so a number never appears anywhere without its interval and its n attached.
    """
    if n == 0 or any(math.isnan(x) for x in (point, lo, hi)):
        return f"n/a, n={n}"
    sign = "+" if (signed and point >= 0) else ""
    return f"{sign}{point:.{digits}f} [{lo:.{digits}f}, {hi:.{digits}f}], n={n}"


def percentile(values: Sequence[float], q: float) -> float:
    """Plain percentile helper (used for the p95 latency invariant)."""
    v = np.asarray([x for x in values if x is not None and not math.isnan(x)], dtype=float)
    if v.size == 0:
        return float("nan")
    return float(np.percentile(v, q))
