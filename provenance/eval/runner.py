"""Eval runner: the CI gate.

What this module is for: loading a golden set, running a pluggable retriever over
it, writing a per-query table, recomputing aggregates from that table, comparing
against a committed baseline, and exiting non-zero on a real regression.

The failures it prevents:

1. **A CI job that quietly starts costing money.** Query embeddings are read from
   a committed fixture, ``eval/fixtures/qvecs_<model_id>.npz``, through the ONE
   canonical frozen implementation in ``provenance.retrieve.embedder``
   (``FrozenEmbedder`` / ``embedding_key`` / ``FIXTURE_PATH_TEMPLATE``). This
   module deliberately owns no cache of its own: it used to, with the same hash
   formula over the *raw* question while the embedder hashed the *BGE-prefixed*
   query, so the two files could never read each other and whichever one CI
   reached for was always missing. On a MISS this raises and the process exits
   non-zero. There is NO live-embedding code path here -- not even behind a flag
   -- because the moment a fallback exists, one bad cache key turns a per-PR gate
   into an API bill and nobody notices for a month. With the fixture, the
   retrieval half of the gate costs exactly $0.00 and finishes in under two
   minutes: no model is loaded, no HTTP call is made, and the only work is a
   Postgres query per record plus some numpy.
2. **Aggregates with nothing behind them.** The per-query table is written first,
   then read back off disk, and the aggregates are recomputed from what was read.
   A headline number in the write-up can therefore always be recomputed from a
   committed artefact; a stored mean that nobody can decompose is not evidence.
3. **A gate that is green because nothing is wired up yet.** An empty golden set,
   an empty index, or the built-in null retriever all report clean zeros and exit
   0 -- while printing loudly that they did so.

Exit codes: 0 pass, 1 regression / invariant failure, 2 hard error (fixture miss,
model or index_version change, unresolved gold anchors).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Annotated, Any, Protocol, cast

import numpy as np
import typer

from provenance.config import settings
from provenance.eval import anchors as anchors_mod
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
)
from provenance.eval.schema import GoldenRecord, load_golden_set
from provenance.graph.tables import CHUNKS, COL_CHUNK_ID, COL_REPO_ID
from provenance.retrieve.embedder import (
    BGE_QUERY_PREFIX,
    FrozenEmbedder,
    FrozenEmbeddingMiss,
    embedding_key,
)
from provenance.retrieve.embedder import (
    fixture_path as _embedder_fixture_path,
)

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_GOLDEN = PACKAGE_DIR / "golden" / "ci_subset.jsonl"
BASELINE_PATH = PACKAGE_DIR / "baselines" / "retrieval.json"
INVARIANT_BASELINE_PATH = PACKAGE_DIR / "baselines" / "invariants.json"

DEFAULT_K = 10
#: Regression threshold on headline recall@10, in absolute points.
DEFAULT_THRESHOLD = 0.02
#: Below this many shared queries the paired test has no power, so the gate falls
#: back to the point estimate alone and says so. See README: on n=40 recall@10
#: quantises to 2.5 points and a 2-point threshold is noise.
MIN_QUERIES_FOR_PAIRED_GATE = 100
#: Non-metric invariant: p95 retrieval latency budget.
P95_LATENCY_BUDGET_MS = 400.0
#: Non-metric invariant: allowed drift in total chunk count between runs.
CHUNK_COUNT_TOLERANCE = 0.01

# Physical names for the two ingest-bookkeeping tables. `provenance.graph.tables`
# covers the query layer; these two are read only here, so they are named once,
# here, and never inline. The plural matters: the previous spellings were
# `ingest_run` and `dead_letter`, which exist in no migration -- see
# _check_invariant_error_policy below for why that was invisible for so long.
INGEST_RUNS = "ingest_runs"
DEAD_LETTERS = "dead_letters"
#: `ingest_status` enum (0001_core): pending|running|succeeded|failed|cancelled.
#: 'ok' is not a member; passing it is `invalid input value for enum`.
INGEST_STATUS_SUCCEEDED = "succeeded"

#: SQLSTATEs that mean "this part of the schema does not exist yet", which is a
#: legitimate week-1 state and may skip. Everything else is a SQL error and must
#: FAIL: undefined_column, invalid enum input, a bad cast and a syntax error are
#: all bugs in *this* file, and a check that cannot fail is not a check.
_SKIPPABLE_SQLSTATES = frozenset(
    {
        "42P01",  # undefined_table
        "42704",  # undefined_object (a missing type/domain the table depends on)
        "3F000",  # invalid_schema_name
    }
)

app = typer.Typer(
    name="eval",
    help="PACE evaluation harness: retrieval gate, invariants, fixture audit.",
    no_args_is_help=True,
    add_completion=False,
)


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def repo_root() -> Path:
    """Find the repo root so ``eval/runs`` and ``eval/fixtures`` land in one place.

    ``PACE_EVAL_ROOT`` overrides it (CI mounts the workspace elsewhere).
    """
    override = os.environ.get("PACE_EVAL_ROOT")
    if override:
        return Path(override).resolve()
    here = PACKAGE_DIR
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    return Path.cwd()


def eval_root() -> Path:
    root = os.environ.get("PACE_EVAL_ROOT")
    return Path(root).resolve() if root else repo_root() / "eval"


# --------------------------------------------------------------------------- #
# Frozen query embeddings -- the $0.00 property
#
# There is exactly ONE implementation of this in the project and it is
# `provenance.retrieve.embedder.FrozenEmbedder`. This module used to carry a
# second one with its own key formula (raw question, not the BGE-prefixed query
# a live embedder actually receives) and its own path convention. Two caches
# that agree on neither key nor path cannot serve each other, so whichever file
# CI reached for was reliably absent -- and "the fixture is missing" is the one
# failure mode that tempts somebody into adding a live fallback. Deleting the
# duplicate is what makes the $0.00 property enforceable rather than aspirational.
# --------------------------------------------------------------------------- #
def fixture_path(model_id: str) -> Path:
    """Delegate to the canonical implementation in provenance.retrieve.embedder.

    This used to be a second copy of the same rule. It agreed with the original
    on both branches -- which is precisely what audit #16 was: two functions that
    agreed until one of them changed. One rule, one place.
    """
    return _embedder_fixture_path(model_id)


def query_fixture_key(model_id: str, question: str) -> str:
    """The .npz key a baker must produce for this question.

    The hashed text is the FINAL text the model would see -- i.e. AFTER the BGE
    query prefix -- because a cache keyed on anything else is not a stand-in for
    the live embedder it replaces.
    """
    return embedding_key(model_id, BGE_QUERY_PREFIX + question)


def load_frozen_queries(model_id: str) -> FrozenEmbedder:
    """Open the committed fixture. Raises ``FrozenEmbeddingMiss`` if it is absent.

    A missing file and a missing key are the same class of failure to the caller
    (bake and commit the vectors), so they are raised as the same exception.
    """
    path = fixture_path(model_id)
    if not path.exists():
        raise FrozenEmbeddingMiss(
            f"no query-vector fixture at {path}. Bake it offline; "
            "`pace eval fixtures` lists the missing questions and their keys. "
            "The runner will not embed live -- that is what keeps CI at $0.00."
        )
    return FrozenEmbedder(path, model_id=model_id)


def missing_query_vectors(frozen: FrozenEmbedder, records: Sequence[GoldenRecord]) -> list[str]:
    """Pre-flight EVERY question and return the qids with no committed vector.

    Pre-flight before scoring, never lazily per query: a miss discovered halfway
    through a run leaves a half-written per-query table whose aggregates look like
    a real (terrible) result. The lookup is a dict hit, so checking all of them
    costs nothing.
    """
    missing: list[str] = []
    for record in records:
        try:
            frozen.encode_query(record.question)
        except FrozenEmbeddingMiss:
            missing.append(record.qid)
    return missing


# --------------------------------------------------------------------------- #
# Retriever plug point
# --------------------------------------------------------------------------- #
class Retriever(Protocol):
    """Anything the gate can score. Returns ranked chunk ids, best first."""

    name: str

    def retrieve(self, question: str, qvec: np.ndarray | None, k: int) -> Sequence[str]: ...


class NullRetriever:
    """Returns nothing. The week-1 default, so the gate is wired before the system.

    Scores a clean 0.0 on everything and refuses nothing -- which is exactly the
    honest reading of "no retriever exists yet".
    """

    name = "null"

    def retrieve(self, question: str, qvec: np.ndarray | None, k: int) -> Sequence[str]:
        return []


def load_retriever(spec: str) -> Retriever:
    """Load ``module:attr``. If the attr is a class or factory, call it."""
    if ":" not in spec:
        raise typer.BadParameter(f"retriever spec must be 'module:attr', got {spec!r}")
    module_name, attr = spec.split(":", 1)
    obj = getattr(import_module(module_name), attr)
    # `isinstance(obj, type)` is load-bearing, not defensive. A class carries
    # `retrieve` as an unbound function, so an attribute test alone reads it as an
    # already-built retriever and hands back the class; every later
    # `retrieve(question, qvec, k)` then binds `question` to `self` and dies on a
    # missing `k`. The default spec is a class, so that path was the only one anyone
    # ever ran.
    is_factory = callable(obj) and not hasattr(obj, "retrieve")
    retriever = obj() if isinstance(obj, type) or is_factory else obj
    if not hasattr(retriever, "retrieve"):
        raise typer.BadParameter(f"{spec} does not provide .retrieve(question, qvec, k)")
    if not hasattr(retriever, "name"):
        retriever.name = attr
    return cast(Retriever, retriever)


# --------------------------------------------------------------------------- #
# Per-query rows
# --------------------------------------------------------------------------- #
PER_QUERY_COLUMNS = (
    "run_id",
    "qid",
    "query_class",
    "k",
    "n_required",
    "n_supporting",
    "n_retrieved",
    "recall_at_k",
    "mrr",
    "ndcg_at_k",
    "citation_precision",
    "refused",
    "expected_refusal",
    "latency_ms",
    "unresolved_anchors",
    "retriever",
    "model",
    "index_version",
)


def _score_one(
    record: GoldenRecord,
    resolved: anchors_mod.ResolvedAnchors,
    retrieved: Sequence[str],
    *,
    k: int,
    latency_ms: float,
    refused: bool,
    citations: Sequence[str],
) -> dict[str, Any]:
    gains = resolved.gains
    return {
        "qid": record.qid,
        "query_class": str(record.query_class),
        "k": k,
        "n_required": len(resolved.required_chunk_ids),
        "n_supporting": len(resolved.supporting_chunk_ids),
        "n_retrieved": len(retrieved),
        "recall_at_k": recall_at_k(retrieved, resolved.required_chunk_ids, k),
        "mrr": mrr(retrieved, resolved.required_chunk_ids, k),
        "ndcg_at_k": ndcg_at_k(retrieved, gains, k),
        "citation_precision": citation_precision(citations, set(gains)),
        "refused": refused,
        "expected_refusal": record.expected_refusal,
        "latency_ms": latency_ms,
        "unresolved_anchors": resolved.n_unresolved,
    }


def write_per_query(rows: Sequence[dict[str, Any]], out_dir: Path) -> Path:
    """Write the per-query table. Parquet when pyarrow is installed, JSONL otherwise.

    pyarrow lives in the optional ``eval`` extra: it is a 40 MB wheel and the gate
    must still run on a machine that only installed the base set. The JSONL
    fallback keeps the same column names, so ``read_per_query`` is agnostic.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        import pyarrow as pa  # noqa: PLC0415 -- optional dependency, imported on use
        import pyarrow.parquet as pq  # noqa: PLC0415
    except ModuleNotFoundError:
        path = out_dir / "per_query.jsonl"
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True, default=str))
                fh.write("\n")
        return path
    path = out_dir / "per_query.parquet"
    table = pa.Table.from_pylist([{c: row.get(c) for c in PER_QUERY_COLUMNS} for row in rows])
    pq.write_table(table, path)
    return path


def read_per_query(path: Path) -> list[dict[str, Any]]:
    """Read back what was just written. Aggregates are computed from THIS, always."""
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq  # noqa: PLC0415

        return [dict(r) for r in pq.read_table(path).to_pylist()]
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def _col(rows: Sequence[dict[str, Any]], name: str) -> list[float]:
    return [float("nan") if r.get(name) is None else float(r[name]) for r in rows]


def aggregate(rows: Sequence[dict[str, Any]], *, seed: int = BOOTSTRAP_SEED) -> dict[str, Any]:
    """Recompute every headline number from per-query rows.

    Stratified by ``query_class`` so the intervals on the refusal metrics are
    computed on resamples that always contain unanswerables.
    """
    if not rows:
        zero = CI(0.0, 0.0, 0.0, 0).as_dict()
        return {
            "n_queries": 0,
            "recall@10": 0.0,
            "recall@10_ci": zero,
            "mrr": 0.0,
            "ndcg@10": 0.0,
            "citation_precision": 0.0,
            "refusal_precision": 0.0,
            "refusal_recall": 0.0,
            "p95_latency_ms": 0.0,
            "unresolved_anchor_queries": 0,
            "per_class": {},
            "per_query": {},
            "per_query_class": {},
            "empty": True,
        }

    strata = [str(r["query_class"]) for r in rows]
    k = int(rows[0].get("k") or DEFAULT_K)
    recall = bootstrap_ci(_col(rows, "recall_at_k"), strata, seed=seed)
    ndcg = bootstrap_ci(_col(rows, "ndcg_at_k"), strata, seed=seed)
    rr = bootstrap_ci(_col(rows, "mrr"), strata, seed=seed)
    cite = bootstrap_ci(_col(rows, "citation_precision"), strata, seed=seed)

    refused = [bool(r.get("refused")) for r in rows]
    unanswerable = [bool(r.get("expected_refusal")) for r in rows]
    r_prec = refusal_precision(refused, unanswerable)
    r_rec = refusal_recall(refused, unanswerable)

    per_class: dict[str, Any] = {}
    for cls in sorted({str(r["query_class"]) for r in rows}):
        subset = [r for r in rows if str(r["query_class"]) == cls]
        ci = bootstrap_ci(_col(subset, "recall_at_k"), None, seed=seed)
        per_class[cls] = {"n": len(subset), f"recall@{k}": ci.point, f"recall@{k}_ci": ci.as_dict()}

    return {
        "n_queries": len(rows),
        f"recall@{k}": recall.point,
        f"recall@{k}_ci": recall.as_dict(),
        "mrr": rr.point,
        "mrr_ci": rr.as_dict(),
        f"ndcg@{k}": ndcg.point,
        f"ndcg@{k}_ci": ndcg.as_dict(),
        "citation_precision": cite.point,
        "citation_precision_ci": cite.as_dict(),
        # NaN (nothing refused / no unanswerables) is reported as 0.0 in the JSON so
        # the file stays valid JSON; the n= fields say whether it means anything.
        "refusal_precision": 0.0 if np.isnan(r_prec) else r_prec,
        "refusal_recall": 0.0 if np.isnan(r_rec) else r_rec,
        "n_refused": sum(refused),
        "n_unanswerable": sum(unanswerable),
        "p95_latency_ms": percentile(_col(rows, "latency_ms"), 95),
        "unresolved_anchor_queries": sum(1 for r in rows if (r.get("unresolved_anchors") or 0) > 0),
        "per_class": per_class,
        # per_query is keyed on qid so the compare step can pair query-for-query;
        # per_query_class travels with it so the paired bootstrap can stratify.
        "per_query": {str(r["qid"]): r.get("recall_at_k") for r in rows},
        "per_query_class": {str(r["qid"]): str(r["query_class"]) for r in rows},
        "empty": False,
    }


# --------------------------------------------------------------------------- #
# Baseline comparison
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Comparison:
    ok: bool
    hard_error: bool
    messages: list[str] = field(default_factory=list)
    delta: float | None = None
    paired: dict[str, Any] | None = None


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {
            "n_queries": 0,
            "recall@10": 0.0,
            "per_query": {},
            "index_version": None,
            "model": None,
        }
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def compare_to_baseline(
    agg: dict[str, Any],
    baseline: dict[str, Any],
    *,
    model: str,
    index_version: str | None,
    threshold: float = DEFAULT_THRESHOLD,
    k: int = DEFAULT_K,
    accept_env_change: bool = False,
) -> Comparison:
    """Gate the run against the committed baseline.

    ``model`` is checked exactly like ``index_version``: a different embedding
    model produces numbers that are not comparable to the stored ones, and a
    silently-accepted model swap is the single easiest way to publish a fake
    improvement.
    """
    cmp = Comparison(ok=True, hard_error=False)
    key = f"recall@{k}"

    for fname, current in (("model", model), ("index_version", index_version)):
        stored = baseline.get(fname)
        if stored is not None and current is not None and stored != current:
            msg = (
                f"{fname} changed: baseline={stored!r} run={current!r}. "
                "The stored numbers are not comparable. Re-baseline deliberately "
                "(`pace eval run --update-baseline`) or pass --accept-env-change."
            )
            if accept_env_change:
                cmp.messages.append(f"WARN  {msg}")
            else:
                cmp.ok = False
                cmp.hard_error = True
                cmp.messages.append(f"FAIL  {msg}")

    if int(baseline.get("n_queries", 0) or 0) == 0:
        cmp.messages.append("INFO  baseline is empty (n_queries=0); nothing to regress against.")
        return cmp

    base_point = float(baseline.get(key, 0.0) or 0.0)
    run_point = float(agg.get(key, 0.0) or 0.0)

    # NaN is not a regression and it is not a pass -- it is an unscored run.
    # recall_at_k returns NaN for a record with no required evidence, which is
    # EVERY record when the index is empty. `NaN < -threshold` evaluates False,
    # so without this guard the gate prints "PASS recall@10 nan" the first day a
    # real baseline is committed. That is the same failure SHAPE as the leakage
    # probe's zero-gold PASS (audit #12): a check that reports success because it
    # measured nothing. Fail loudly instead.
    if math.isnan(run_point):
        cmp.ok = False
        cmp.delta = float("nan")
        cmp.messages.append(
            f"FAIL  {key} is NaN -- the run scored nothing. An empty or unresolvable "
            "index produces NaN for every query; that is an unscored run, not a "
            "passing one. Check that `pace ingest` populated chunks and that gold "
            "anchors resolve."
        )
        return cmp

    cmp.delta = run_point - base_point

    shared = sorted(set(agg.get("per_query", {})) & set(baseline.get("per_query", {})))
    if shared:
        sys_vals = [agg["per_query"][q] for q in shared]
        base_vals = [baseline["per_query"][q] for q in shared]
        classes = agg.get("per_query_class", {})
        strata = [classes.get(q, "unknown") for q in shared]
        paired = paired_bootstrap(sys_vals, base_vals, strata)
        cmp.paired = paired.as_dict()
        cmp.messages.append(
            f"INFO  paired delta {paired} prob_not_better={paired.prob_not_better:.3f}"
        )

    if cmp.delta < -threshold:
        underpowered = len(shared) < MIN_QUERIES_FOR_PAIRED_GATE
        if underpowered:
            cmp.messages.append(
                f"WARN  only {len(shared)} shared queries: below {MIN_QUERIES_FOR_PAIRED_GATE} the "
                f"gate cannot separate a regression from one query flipping "
                f"(recall@{k} quantises to {100 / max(len(shared), 1):.1f} points)."
            )
            cmp.ok = False
            cmp.messages.append(
                f"FAIL  {key} {run_point:.4f} vs baseline {base_point:.4f} "
                f"(delta {cmp.delta:+.4f}, threshold {threshold:.4f})"
            )
        elif cmp.paired is not None and not cmp.paired["significant"]:
            cmp.messages.append(
                f"WARN  {key} dropped {cmp.delta:+.4f} but the paired interval includes 0; "
                "not gating on noise."
            )
        else:
            cmp.ok = False
            cmp.messages.append(
                f"FAIL  {key} regression {cmp.delta:+.4f} exceeds threshold {threshold:.4f} "
                f"and the paired interval excludes 0."
            )
    else:
        cmp.messages.append(f"PASS  {key} {run_point:.4f} (delta {cmp.delta:+.4f})")
    return cmp


# --------------------------------------------------------------------------- #
# Non-metric invariants -- these catch most real bugs for free
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class InvariantResult:
    name: str
    status: str  # pass | fail | skip
    detail: str

    @property
    def failed(self) -> bool:
        return self.status == "fail"


@contextmanager
def _own_transaction(conn: Any) -> Iterator[None]:
    """Run one statement in its own transaction.

    WHY THIS EXISTS: the checks below used to share the caller's transaction. The
    first one referenced a table that does not exist, Postgres aborted the
    transaction, and every later check died with InFailedSqlTransaction -- which
    the old blanket handler turned into "skip". One typo therefore silenced the
    whole invariant suite, permanently and silently. psycopg's ``transaction()``
    is a real transaction at the top level and a SAVEPOINT inside one, so a
    failure here can never cascade into the next check either way.
    """
    begin = getattr(conn, "transaction", None)
    if begin is None:  # a test double with only .execute()
        yield
        return
    with begin():
        yield


def _scalar(conn: Any, sql: str, params: Any = None) -> Any:
    with _own_transaction(conn):
        row = conn.execute(sql, params).fetchone()
    # Rows are TUPLES: graph.db.connection defaults to tuple_row.
    return None if row is None else row[0]


def _check_invariant_error_policy(exc: Exception) -> str:
    """ "skip" for a schema that does not exist yet; "fail" for a SQL error.

    A CHECK THAT CANNOT FAIL IS NOT A CHECK. Every one of these used to be
    wrapped in ``except Exception -> skip``, so `FROM ingest_run`, `status = 'ok'`
    and `FROM dead_letter` -- three identifiers that match nothing in any
    migration -- reported "skip" on every run since the file was written, and the
    T1 idempotency and dead-letter gates could never go red. Only "the table is
    not there yet" is tolerable; a syntax error, a bad column, a bad enum value or
    a bad cast is a bug in this file and must be loud.

    ``sqlstate`` is read off the exception rather than importing psycopg, so this
    module still imports on a machine with no database driver.
    """
    sqlstate = getattr(exc, "sqlstate", None)
    return "skip" if sqlstate in _SKIPPABLE_SQLSTATES else "fail"


def check_invariants(
    conn: Any,
    rows: Sequence[dict[str, Any]],
    *,
    repo_id: int,
    baseline: dict[str, Any] | None = None,
    strict: bool = False,
) -> list[InvariantResult]:
    """Cheap structural checks that fail loudly when ingest is wrong.

    None of these are metrics. They are the checks that, in practice, catch the
    bugs a recall number never will: a re-ingest that duplicates rows, a chunker
    that became non-deterministic, an error path that swallows failures, a query
    plan that fell off an index.

    Missing tables report ``skip`` rather than ``fail`` so that week-1 CI is green
    before the ingest schema exists; a SQL *error* reports ``fail``, because that
    is a bug in this file rather than a state of the world (see
    ``_check_invariant_error_policy``). ``--strict-invariants`` turns skips into
    failures once the schema is there.
    """
    baseline = baseline or {}
    results: list[InvariantResult] = []

    def record(name: str, status: str, detail: str) -> None:
        if status == "skip" and strict:
            status = "fail"
        results.append(InvariantResult(name, status, detail))

    def failed(name: str, what: str, exc: Exception) -> None:
        status = _check_invariant_error_policy(exc)
        prefix = f"{what} not present yet" if status == "skip" else f"SQL error against {what}"
        record(name, status, f"{prefix}: {type(exc).__name__}: {exc}")

    # 1. A second ingest of an unchanged corpus must write nothing. If it writes,
    #    identity is unstable (usually: something non-deterministic leaked into
    #    chunk_id, or upserts are inserting).
    try:
        net = _scalar(
            conn,
            f"""
            SELECT coalesce(rows_inserted, 0) + coalesce(rows_updated, 0)
                 + coalesce(rows_deleted, 0)
            FROM {INGEST_RUNS}
            WHERE {COL_REPO_ID} = %(repo_id)s AND status = %(status)s::ingest_status
            ORDER BY started_at DESC
            LIMIT 1
            """,
            {"repo_id": repo_id, "status": INGEST_STATUS_SUCCEEDED},
        )
        if net is None:
            record(
                "second_ingest_zero_net_writes",
                "skip",
                f"no {INGEST_STATUS_SUCCEEDED} {INGEST_RUNS} rows for repo_id={repo_id} yet",
            )
        else:
            record(
                "second_ingest_zero_net_writes",
                "pass" if int(net) == 0 else "fail",
                f"last ingest net writes = {int(net)} (expected 0 on an unchanged corpus)",
            )
    except Exception as exc:  # noqa: BLE001 -- classified, not swallowed
        failed("second_ingest_zero_net_writes", INGEST_RUNS, exc)

    # 2. Chunk-id set hash. Same corpus + same chunker version => same id set.
    try:
        digest = _scalar(
            conn,
            f"""
            SELECT encode(
                       sha256(convert_to(
                           coalesce(string_agg({COL_CHUNK_ID}, ',' ORDER BY {COL_CHUNK_ID}), ''),
                           'UTF8')),
                       'hex')
            FROM {CHUNKS} WHERE {COL_REPO_ID} = %s
            """,
            (repo_id,),
        )
        expected = baseline.get("chunk_id_set_sha256")
        if expected is None:
            record("chunk_id_set_hash", "skip", f"no stored hash; current = {digest}")
        else:
            record(
                "chunk_id_set_hash",
                "pass" if digest == expected else "fail",
                f"current={digest} stored={expected}",
            )
    except Exception as exc:  # noqa: BLE001
        failed("chunk_id_set_hash", CHUNKS, exc)

    # 3. Dead-letter table empty. A non-empty dead letter queue means ingest
    #    "succeeded" while dropping documents -- silently shrinking the corpus.
    try:
        n_dead = _scalar(
            conn,
            f"SELECT count(*) FROM {DEAD_LETTERS} WHERE {COL_REPO_ID} = %s AND resolved_at IS NULL",
            (repo_id,),
        )
        record(
            "dead_letter_empty",
            "pass" if int(n_dead or 0) == 0 else "fail",
            f"{int(n_dead or 0)} unresolved {DEAD_LETTERS} rows for repo_id={repo_id}",
        )
    except Exception as exc:  # noqa: BLE001
        failed("dead_letter_empty", DEAD_LETTERS, exc)

    # 4. p95 retrieval latency, measured from this run's own per-query timings.
    p95 = percentile(_col(rows, "latency_ms"), 95) if rows else 0.0
    if rows:
        record(
            "p95_retrieval_latency",
            "pass" if p95 <= P95_LATENCY_BUDGET_MS else "fail",
            f"p95={p95:.1f} ms (budget {P95_LATENCY_BUDGET_MS:.0f} ms)",
        )
    else:
        record("p95_retrieval_latency", "skip", "no queries scored")

    # 5. Chunk count within +/-1% of the stored count. Catches a chunker change
    #    that quietly halves or doubles the corpus.
    try:
        count = int(
            _scalar(
                conn,
                f"SELECT count(*) FROM {CHUNKS} WHERE {COL_REPO_ID} = %s",
                (repo_id,),
            )
            or 0
        )
        expected_count = baseline.get("chunk_count")
        if not expected_count:
            record("chunk_count_stable", "skip", f"no stored count; current = {count}")
        else:
            drift = abs(count - int(expected_count)) / int(expected_count)
            record(
                "chunk_count_stable",
                "pass" if drift <= CHUNK_COUNT_TOLERANCE else "fail",
                f"count={count} stored={expected_count} drift={drift:.3%} "
                f"(tolerance {CHUNK_COUNT_TOLERANCE:.0%})",
            )
    except Exception as exc:  # noqa: BLE001
        failed("chunk_count_stable", CHUNKS, exc)

    return results


# --------------------------------------------------------------------------- #
# Scoring helpers -- everything that needs a live connection lives in here, so
# `run()` can hold exactly one `with connection() as conn:` block around them.
# --------------------------------------------------------------------------- #
def _repo_id(conn: Any) -> int | None:
    """``settings.repo_owner``/``repo_name`` -> ``repositories.repo_id`` (bigint).

    Returns None when the corpus has not been ingested into this database, which
    is a legitimate pre-ingest state and scores clean zeros.

    NEVER build ``f"{owner}/{name}"`` and hand it to a query: every repo_id column
    in the schema is bigint, so the slug raises `invalid input syntax for type
    bigint` -- and it does so only after the fixture load and the scoring loop
    have already run, which is a long way to travel for a type error.
    """
    from provenance.graph.repos import UnknownRepository, resolve_repo_id  # noqa: PLC0415

    try:
        return resolve_repo_id(conn, settings.repo_owner, settings.repo_name)
    except UnknownRepository:
        return None


def _resolve_gold(
    conn: Any, records: Sequence[GoldenRecord], repo_id: int | None
) -> dict[str, anchors_mod.ResolvedAnchors]:
    """Gold anchors -> current chunk ids, or clean zeros before the first ingest."""
    empty = {r.qid: anchors_mod.ResolvedAnchors(qid=r.qid) for r in records}
    if repo_id is None:
        typer.echo(
            f"[eval] {settings.repo_owner}/{settings.repo_name} is not in "
            "`repositories` -> reporting zeros (expected before the first ingest)."
        )
        return empty
    try:
        populated = anchors_mod.index_is_populated(conn, repo_id)
    except Exception as exc:  # noqa: BLE001 -- classified below, never swallowed
        conn.rollback()  # leave the connection usable for the invariant checks
        if _check_invariant_error_policy(exc) == "skip":
            typer.echo(f"[eval] chunk schema not present yet ({exc}); reporting zeros.")
            return empty
        # Anything else means the resolver and the schema disagree. Scoring on
        # anyway would give every answerable query a 0.0 and read as a total
        # retrieval failure -- the exact silent-wrong-number this gate exists to
        # prevent. Die instead.
        typer.secho(
            f"[eval] anchor resolution FAILED against the live schema: {exc}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2) from exc
    if not populated:
        typer.echo("[eval] index is EMPTY -> reporting zeros (expected before first ingest).")
        return empty
    return anchors_mod.resolve_all(records, conn, repo_id=repo_id)


def _score_records(
    records: Sequence[GoldenRecord],
    resolved_map: dict[str, anchors_mod.ResolvedAnchors],
    retriever: Retriever,
    frozen: FrozenEmbedder | None,
    *,
    k: int,
    rid: str,
    model_id: str,
    index_version: str | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        # Pre-flighted above, so this cannot miss; if it somehow does, it raises
        # FrozenEmbeddingMiss rather than embedding anything.
        qvec = frozen.encode_query(record.question) if frozen is not None else None
        t0 = time.perf_counter()
        retrieved = list(retriever.retrieve(record.question, qvec, k))
        latency_ms = (time.perf_counter() - t0) * 1000.0
        # Tier 1 has no generator, so "refusal" is the retriever returning nothing
        # and "citations" are the retrieved chunk ids. The e2e tier overrides both
        # with the agent's actual refusal decision and its actual citation list.
        row = _score_one(
            record,
            resolved_map[record.qid],
            retrieved,
            k=k,
            latency_ms=latency_ms,
            refused=not retrieved,
            citations=retrieved[:k],
        )
        row.update(
            {
                "run_id": rid,
                "retriever": retriever.name,
                "model": model_id,
                "index_version": index_version,
            }
        )
        rows.append(row)
    return rows


def _fail_on_unresolved(
    resolved_map: dict[str, anchors_mod.ResolvedAnchors], *, allow_unresolved: bool
) -> None:
    unresolved = {
        qid: res.unresolved_required for qid, res in resolved_map.items() if res.unresolved_required
    }
    if unresolved and not allow_unresolved:
        typer.secho(
            f"[eval] {len(unresolved)} record(s) have required anchors that resolve to no chunk. "
            "That is a scoring/ingest bug, not a retrieval miss -- it must never be counted as a "
            f"zero. First: {list(unresolved)[:3]}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _new_run_id(model: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{hashlib.sha256(model.encode()).hexdigest()[:8]}"


def _emit(agg: dict[str, Any], k: int) -> None:
    typer.echo("")
    typer.echo(f"  n_queries          {agg['n_queries']}")
    for name in (f"recall@{k}", "mrr", f"ndcg@{k}", "citation_precision"):
        ci = agg.get(f"{name}_ci")
        if ci:
            typer.echo(f"  {name:<18} {format_metric(ci['point'], ci['lo'], ci['hi'], ci['n'])}")
    typer.echo(
        f"  refusal            P={agg['refusal_precision']:.2f} R={agg['refusal_recall']:.2f} "
        f"(refused {agg.get('n_refused', 0)}/{agg['n_queries']}, "
        f"unanswerable {agg.get('n_unanswerable', 0)})"
    )
    typer.echo(f"  p95 latency        {agg['p95_latency_ms']:.1f} ms")
    typer.echo("")


@app.command("run")
def run(
    golden: Annotated[Path, typer.Option(help="Golden set .jsonl.")] = DEFAULT_GOLDEN,
    retriever_spec: Annotated[
        str, typer.Option("--retriever", help="module:attr providing .retrieve(question, qvec, k)")
    ] = "provenance.eval.runner:NullRetriever",
    k: Annotated[int, typer.Option(help="Retrieval cut-off.")] = DEFAULT_K,
    model: Annotated[
        str | None, typer.Option(help="Embedding model id; defaults to settings.")
    ] = None,
    index_version: Annotated[
        str | None, typer.Option(help="Index version tag for this run.")
    ] = None,
    embeddings: Annotated[
        str, typer.Option(help="'frozen' (fixture .npz) or 'none' (lexical retrievers).")
    ] = "frozen",
    threshold: Annotated[
        float, typer.Option(help="Recall regression threshold.")
    ] = DEFAULT_THRESHOLD,
    use_db: Annotated[
        bool, typer.Option("--db/--no-db", help="Resolve anchors against Postgres.")
    ] = True,
    check_invariants_flag: Annotated[
        bool, typer.Option("--invariants/--no-invariants", help="Run the non-metric checks.")
    ] = True,
    strict_invariants: Annotated[
        bool, typer.Option(help="Treat skipped invariants as failures.")
    ] = False,
    allow_unresolved: Annotated[
        bool, typer.Option(help="Do not hard-fail when gold anchors resolve to nothing.")
    ] = False,
    accept_env_change: Annotated[
        bool, typer.Option(help="Downgrade model/index mismatch to a warning.")
    ] = False,
    update_baseline: Annotated[
        bool, typer.Option(help="Overwrite baselines/retrieval.json.")
    ] = False,
    run_id: Annotated[str | None, typer.Option(help="Override the run id.")] = None,
    runs_dir: Annotated[
        Path | None, typer.Option(help="Where to write eval/runs/<run_id>/.")
    ] = None,
) -> None:
    """Score a golden set and gate on regression against the committed baseline."""
    model_id = model or settings.embedding_model_version
    rid = run_id or _new_run_id(model_id)
    out_dir = (runs_dir or (eval_root() / "runs")) / rid

    records = load_golden_set(golden)
    typer.echo(f"[eval] run_id={rid} model={model_id} golden={golden} n={len(records)}")

    # --- empty golden set: clean zeros, exit 0. Week 1 must be green. ---------
    if not records:
        typer.echo("[eval] golden set is EMPTY -> reporting zeros (this is expected in week 1).")
        agg = aggregate([])
        path = write_per_query([], out_dir)
        _finish(out_dir, rid, agg, [], model_id, index_version, path, [])
        _emit(agg, k)
        raise typer.Exit(0)

    retriever = load_retriever(retriever_spec)

    # --- frozen query vectors: hard failure on a miss, never a live call ------
    frozen: FrozenEmbedder | None = None
    if embeddings == "frozen":
        try:
            frozen = load_frozen_queries(model_id)
            # Pre-flight EVERY question before a single one is scored.
            missing = missing_query_vectors(frozen, records)
            if missing:
                raise FrozenEmbeddingMiss(
                    f"{len(missing)} question(s) missing from {fixture_path(model_id)}: "
                    f"{', '.join(missing[:5])}{'...' if len(missing) > 5 else ''}. "
                    "Bake and commit the vectors -- there is no live fallback."
                )
        except FrozenEmbeddingMiss as exc:
            typer.secho(f"[eval] FIXTURE MISS: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc
    elif embeddings != "none":
        raise typer.BadParameter("--embeddings must be 'frozen' or 'none' (there is no live mode)")

    # --- anchor resolution, scoring and invariants, on ONE pooled connection --
    invariants: list[InvariantResult] = []

    def score(resolved_map: dict[str, anchors_mod.ResolvedAnchors]) -> list[dict[str, Any]]:
        return _score_records(
            records,
            resolved_map,
            retriever,
            frozen,
            k=k,
            rid=rid,
            model_id=model_id,
            index_version=index_version,
        )

    if use_db:
        from provenance.graph.db import connection  # noqa: PLC0415 -- keeps --help DB-free

        # `with connection() as conn`, never `ctx = connection(); ctx.__enter__()`.
        # The old code entered the context manually, never exited it, and called
        # conn.close() on the way out -- which closes a POOLED connection behind
        # the pool's back. The pool goes on believing it is live and hands the
        # dead handle to whoever checks out next.
        with connection() as conn:
            repo_id = _repo_id(conn)
            resolved_map = _resolve_gold(conn, records, repo_id)
            _fail_on_unresolved(resolved_map, allow_unresolved=allow_unresolved)
            # Close the read transaction before the scoring loop, which can run
            # for minutes: an idle-in-transaction connection pins a snapshot and
            # holds back autovacuum for as long as the whole run takes. Nothing
            # was written, so there is nothing to lose.
            conn.rollback()
            rows = score(resolved_map)
            if check_invariants_flag and repo_id is not None:
                invariants = check_invariants(
                    conn,
                    rows,
                    repo_id=repo_id,
                    baseline=_load_json(INVARIANT_BASELINE_PATH),
                    strict=strict_invariants,
                )
            elif check_invariants_flag:
                typer.echo("[eval] invariants skipped: the corpus is not in `repositories` yet.")
    else:
        resolved_map = {r.qid: anchors_mod.ResolvedAnchors(qid=r.qid) for r in records}
        rows = score(resolved_map)

    # Write first, then read back, then aggregate from what is on disk.
    per_query_path = write_per_query(rows, out_dir)
    agg = aggregate(read_per_query(per_query_path))

    baseline = load_baseline()
    cmp = compare_to_baseline(
        agg,
        baseline,
        model=model_id,
        index_version=index_version,
        threshold=threshold,
        k=k,
        accept_env_change=accept_env_change,
    )

    _finish(out_dir, rid, agg, invariants, model_id, index_version, per_query_path, cmp.messages)
    _emit(agg, k)
    for line in cmp.messages:
        typer.echo(f"[eval] {line}")
    for inv in invariants:
        typer.echo(f"[inv]  {inv.status.upper():<4} {inv.name}: {inv.detail}")

    if update_baseline:
        new_baseline = {
            "n_queries": agg["n_queries"],
            f"recall@{k}": agg.get(f"recall@{k}", 0.0),
            "per_query": agg["per_query"],
            "index_version": index_version,
            "model": model_id,
            "run_id": rid,
            "bootstrap_seed": BOOTSTRAP_SEED,
        }
        BASELINE_PATH.write_text(
            json.dumps(new_baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        typer.echo(f"[eval] baseline updated: {BASELINE_PATH}")

    # No conn.close() here: the connection was checked out with `with
    # connection()` and has already gone back to the pool.
    if cmp.hard_error:
        raise typer.Exit(2)
    if not cmp.ok or any(i.failed for i in invariants):
        raise typer.Exit(1)
    raise typer.Exit(0)


@app.command("fixtures")
def fixtures(
    golden: Annotated[Path, typer.Option(help="Golden set .jsonl.")] = DEFAULT_GOLDEN,
    model: Annotated[str | None, typer.Option(help="Embedding model id.")] = None,
) -> None:
    """Audit the frozen query-vector cache: which questions have no committed vector.

    Reports only. Baking vectors is an offline, deliberate act -- it is the one
    place an embedding model actually runs, and it must never happen inside CI.
    """
    model_id = model or settings.embedding_model_version
    records = load_golden_set(golden)
    path = fixture_path(model_id)
    typer.echo(f"[eval] fixture {path} (exists={path.exists()}) questions={len(records)}")
    if not path.exists():
        # The keys printed here are the ones the baker must produce: hashed over
        # the BGE-prefixed query, exactly as FrozenEmbedder will look them up.
        for r in records:
            typer.echo(f"  MISS {r.qid} key={query_fixture_key(model_id, r.question)}")
        raise typer.Exit(1 if records else 0)
    cache = load_frozen_queries(model_id)
    missing = missing_query_vectors(cache, records)
    typer.echo(f"[eval] vectors={len(cache)} missing={len(missing)}")
    by_qid = {r.qid: r for r in records}
    for qid in missing:
        typer.echo(f"  MISS {qid} key={query_fixture_key(model_id, by_qid[qid].question)}")
    raise typer.Exit(1 if missing else 0)


@app.command("invariants")
def invariants_cmd(
    strict: Annotated[bool, typer.Option(help="Treat skipped checks as failures.")] = False,
) -> None:
    """Run the non-metric invariant checks on their own (no golden set needed)."""
    from provenance.graph.db import connection  # noqa: PLC0415

    with connection() as conn:
        repo_id = _repo_id(conn)
        if repo_id is None:
            typer.echo(
                f"[inv] {settings.repo_owner}/{settings.repo_name} is not in `repositories`: "
                "there is nothing ingested to check."
            )
            raise typer.Exit(1 if strict else 0)
        results = check_invariants(
            conn,
            [],
            repo_id=repo_id,
            baseline=_load_json(INVARIANT_BASELINE_PATH),
            strict=strict,
        )
    for inv in results:
        typer.echo(f"[inv] {inv.status.upper():<4} {inv.name}: {inv.detail}")
    raise typer.Exit(1 if any(i.failed for i in results) else 0)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _finish(
    out_dir: Path,
    rid: str,
    agg: dict[str, Any],
    invariants: Sequence[InvariantResult],
    model_id: str,
    index_version: str | None,
    per_query_path: Path,
    messages: Sequence[str],
) -> None:
    """Write the run manifest next to the per-query table."""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": rid,
        "created_at": datetime.now(UTC).isoformat(),
        "model": model_id,
        "index_version": index_version,
        "embedding_dim": settings.embedding_dim,
        "chunking_strategy_version": settings.chunking_strategy_version,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "per_query_file": per_query_path.name,
        "python": sys.version.split()[0],
        # Aggregates are a *derived view* of per_query_file and are recomputed on
        # every read; never treat this block as the source of truth.
        "aggregates": agg,
        "invariants": [asdict(i) for i in invariants],
        "gate_messages": list(messages),
    }
    (out_dir / "run.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Direct entry point: ``python -m provenance.eval.runner run --no-db``.

    ``argv`` is EXPLICIT. The canonical mount is
    ``typer.add_typer(runner.app, name="eval")`` in ``provenance/cli.py``, but
    while a caller invoked this with no argument Click re-read ``sys.argv[1:]``
    -- which still began with the word ``eval`` -- and every single
    ``pace eval <anything>`` died with "No such command 'eval'". A CLI entry
    point that silently re-reads global state cannot be composed.
    """
    app(args=list(argv) if argv is not None else None)


if __name__ == "__main__":
    main()
