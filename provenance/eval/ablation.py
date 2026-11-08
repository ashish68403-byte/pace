"""Checkpoint B's ablation table, through the runner's own scoring (slices B5a and B5b).

`pace eval ablation` scores six rows on the retained dev records: grep and the leakage
probe's identifier-aware BM25 (`bm25_ident`, the reference every lift is quoted against,
ADR-0003 §6); the production BM25 index as it read identifiers before B5b
(`bm25_phrase`) and after (`bm25`); exact dense retrieval (`dense`); and their reciprocal
rank fusion (`rrf`) at the settings tasks/eval-harness/007 fixed before any dense number.
Every row goes through `runner._score_records` and `runner.aggregate`. Each difference is
quoted twice, raw and divided by the gold's validity (ADR-0004 §8). Nothing is written
unless grep's and bm25_ident's hits equal the leakage verdict's, every pool document has
its stored vector and those vectors are the ones `pace pool embed` recorded, and every
scored question has a frozen vector.
"""

from __future__ import annotations

import gc
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from provenance.eval import runner
from provenance.eval.metrics import discordant, mde, paired_bootstrap
from provenance.retrieve.fusion import DEFAULT_RRF_K

SCHEMA: Final = "pace.ablation/v2"
K: Final = 10
SPLIT: Final = "dev"
REFERENCE: Final = "bm25_ident"
#: The rows the leakage verdict also measured, which the cross-check compares.
CHECKED: Final = ("grep", "bm25_ident")
#: The table's rows, in the order the summary prints them.
ROWS: Final = ("grep", "bm25_ident", "bm25_phrase", "bm25", "dense", "rrf")
#: The paired comparisons B5b asks for, beside every row against the reference: the fix,
#: what dense adds to BM25, and what BM25 adds to dense.
PAIRS: Final = (("bm25", "bm25_phrase"), ("rrf", "bm25"), ("rrf", "dense"))
#: rrf's k, swept for sensitivity only: tasks/eval-harness/007 fixed k = 60 in advance.
SENSITIVITY_K: Final = (10, 20, 30, 60, 100, 200)
SENSITIVITY_NOTE: Final = "sensitivity only; k = 60 was fixed in the spec"
DEFAULT_EMBEDDING_RECORD: Final = Path("results") / "pool_embedding.json"
DEFAULT_GOLDEN: Final = Path("provenance") / "eval" / "golden" / "auto_full.jsonl"
DEFAULT_OUT: Final = Path("results") / "ablation.json"
DEFAULT_PER_QUERY: Final = Path("results") / "ablation_per_query.jsonl"
DEFAULT_VERDICT: Final = Path("results") / "leakage_verdict.json"
DEFAULT_AUDIT: Final = Path("results") / "gold_validity_audit.json"

Rows = Mapping[str, Sequence[Mapping[str, Any]]]


class AblationCheckFailed(RuntimeError):
    """The runner and the leakage probe disagree, or the verdict describes another run."""


def hits_at_k(rows: Sequence[Mapping[str, Any]]) -> int:
    """Records with any gold document in the top k: the probe's hit, counted from rows."""
    return sum(1 for row in rows if row.get("first_gold_rank") is not None)


def sensitivity_point(k: int, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The fused row's hits and recall@10 at one k, through the runner's own aggregate."""
    agg = runner.aggregate([dict(row) for row in rows])
    return {"k": k, "hits@10": hits_at_k(rows), "recall@10": agg[f"recall@{K}_ci"]["point"]}


def row_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One table row, through the runner's own `aggregate`. No latency: it is not quality."""
    agg = runner.aggregate([dict(row) for row in rows])
    return {
        "hits@10": hits_at_k(rows),
        "recall@10": agg[f"recall@{K}_ci"],
        "mrr@10": agg["mrr_ci"],
        "ndcg@10": agg[f"ndcg@{K}_ci"],
        "per_class": agg["per_class"],
        "per_symbol_df": agg["per_symbol_df"],
    }


def _recall(row: Mapping[str, Any]) -> float:
    value = row.get("recall_at_k")
    return float("nan") if value is None else float(value)


def compare(
    system: Sequence[Mapping[str, Any]],
    reference: Sequence[Mapping[str, Any]],
    validity: Mapping[str, Any],
) -> dict[str, Any]:
    """A row's paired difference in recall@10 against the reference, quoted twice."""
    ref = {str(row["qid"]): row for row in reference}
    own = {str(row["qid"]): row for row in system}
    qids = sorted(set(own) & set(ref))
    a = [_recall(own[q]) for q in qids]
    b = [_recall(ref[q]) for q in qids]
    paired = paired_bootstrap(a, b, [str(ref[q]["query_class"]) for q in qids])
    d, n = discordant(a, b)
    out: dict[str, Any] = {
        **paired.as_dict(),
        "discordant": d,
        "mde": None if d == 0 else mde(n, d / n),
    }
    if validity.get("status") == "current":
        rate = float(validity["rate"])
        low, high = (float(x) for x in validity["ci95"])
        if min(rate, low, high) > 0:
            # ADR-0004 §8: the raw difference is the claim; divided, it shows how much a
            # benchmark with partly empty gold may hide. A lower validity makes it larger.
            out["over_validity"] = {
                "at_point": paired.delta / rate,
                "at_upper": paired.delta / high,
                "at_lower": paired.delta / low,
            }
    return out


def read_validity(path: Path, pool_version: str) -> dict[str, Any]:
    """The gold's validity from the audit's report (ADR-0004), or why it cannot be used."""
    if not path.exists():
        return {"status": "missing", "source": path.as_posix()}
    report = json.loads(path.read_text(encoding="utf-8"))
    labels = report.get("labels", {})
    if labels.get("status") != "complete":
        return {"status": "incomplete", "source": path.as_posix()}
    measured_on = report.get("sample", {}).get("pool_version")
    if measured_on != pool_version:
        return {"status": "stale", "source": path.as_posix(), "pool_version": measured_on}
    overall = labels["overall"]
    return {
        "status": "current",
        "source": path.as_posix(),
        "rate": overall["rate"],
        "ci95": overall["ci95"],
    }


def cross_check(
    verdict_path: Path, hits: Mapping[str, int], *, records: int, pool_size: int
) -> dict[str, Any]:
    """grep's and bm25_ident's hits must equal the verdict's, on the same records and pool."""
    if not verdict_path.exists():
        raise AblationCheckFailed(f"{verdict_path} does not exist: run the leakage verdict first")
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    scored = (verdict.get("n_retained"), verdict.get("pool_size"))
    if scored != (records, pool_size):
        raise AblationCheckFailed(
            f"the verdict scored {scored[0]} records over {scored[1]} documents, this run "
            f"{records} over {pool_size}: regenerate the verdict first"
        )
    baselines = {b["name"]: b for b in verdict.get("baselines", [])}
    checked: dict[str, Any] = {"source": verdict_path.as_posix()}
    for name in CHECKED:
        expected = int(baselines[name]["hits_at_10"])
        if hits[name] != expected:
            raise AblationCheckFailed(
                f"{name}: the runner found {hits[name]} hits at 10 and the verdict {expected}; "
                "the two computations disagree"
            )
        checked[name] = expected
    return checked


def check_query_vectors(missing: Sequence[str]) -> None:
    """Every scored question needs its frozen vector: a miss would rank dense on nothing."""
    if missing:
        raise AblationCheckFailed(
            f"{len(missing):,} scored questions have no frozen vector (first: "
            f"{', '.join(missing[:3])}): run `pace eval bake`"
        )


def check_embedding(
    record_path: Path, *, model: str, fingerprint: str, missing: int
) -> dict[str, Any]:
    """The stored vectors must be complete, and the ones `pace pool embed` recorded."""
    if missing:
        raise AblationCheckFailed(
            f"{missing:,} pool documents have no current vector for {model}: run `pace pool embed`"
        )
    if not record_path.exists():
        raise AblationCheckFailed(f"{record_path} does not exist: run `pace pool embed` first")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    recorded = record.get("model", {}).get("model")
    if recorded != model:
        raise AblationCheckFailed(f"{record_path} describes {recorded}, not {model}")
    if record.get("fingerprint") != fingerprint:
        raise AblationCheckFailed(
            f"the stored vectors' fingerprint is not the one {record_path} recorded: they "
            "changed after `pace pool embed` wrote it"
        )
    return {"source": record_path.as_posix(), "model": model, "fingerprint": fingerprint}


def build_table(
    rows: Rows,
    *,
    meta: Mapping[str, Any],
    validity: Mapping[str, Any],
    checked: Mapping[str, Any],
    sensitivity: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """`results/ablation.json`: every row, every comparison with the reference, the pairs."""
    if REFERENCE not in rows:
        raise ValueError(f"the reference row {REFERENCE} is missing")
    table: dict[str, Any] = {
        "schema": SCHEMA,
        **meta,
        "k": K,
        "split": SPLIT,
        "reference": REFERENCE,
        "rows": {name: row_summary(rows[name]) for name in rows},
        "comparisons": {
            name: compare(rows[name], rows[REFERENCE], validity)
            for name in rows
            if name != REFERENCE
        },
        "pairs": {
            f"{system}_vs_{baseline}": {
                "system": system,
                "baseline": baseline,
                **compare(rows[system], rows[baseline], validity),
            }
            for system, baseline in PAIRS
            if system in rows and baseline in rows
        },
        "validity": dict(validity),
        "cross_check": dict(checked),
    }
    if sensitivity is not None:
        table["rrf_k_sensitivity"] = {
            "note": SENSITIVITY_NOTE,
            "fixed_k": DEFAULT_RRF_K,
            "points": [dict(point) for point in sensitivity],
        }
    return table


def per_query_lines(rows: Rows) -> list[str]:
    """`results/ablation_per_query.jsonl`: each record's first gold rank in every row."""
    by_qid: dict[str, dict[str, Any]] = {}
    for name in sorted(rows):
        for row in rows[name]:
            qid = str(row["qid"])
            entry = by_qid.setdefault(
                qid,
                {
                    "qid": qid,
                    "query_class": row["query_class"],
                    "gold_symbol_df": row.get("gold_symbol_df"),
                    "first_gold_rank": {},
                },
            )
            entry["first_gold_rank"][name] = row.get("first_gold_rank")
    return [json.dumps(by_qid[qid], sort_keys=True) + "\n" for qid in sorted(by_qid)]


def render_json(table: Mapping[str, Any]) -> str:
    """Sorted keys, no NaN, no timings: the same database gives the same bytes."""
    return json.dumps(table, indent=2, sort_keys=True, allow_nan=False) + "\n"


def _versus(c: Mapping[str, Any]) -> str:
    mde_text = "n/a" if c["mde"] is None else f"{c['mde']:.3f}"
    interval = f"{c['delta']:+.3f} [{c['lo']:+.3f}, {c['hi']:+.3f}]"
    return f"{interval}, D={c['discordant']}, MDE={mde_text}"


def render_summary(table: Mapping[str, Any], out: Path, per_query_out: Path) -> str:
    lines = [
        f"ablation on {table['pool_version']}: {table['records']:,} retained {table['split']} "
        f"records, k = {table['k']}, reference {table['reference']}",
        f"  {'row':<12} {'recall@10 [95% CI]':<22} {'MRR@10':>7} {'nDCG@10':>8}   versus reference",
    ]
    names = [n for n in ROWS if n in table["rows"]] + sorted(set(table["rows"]) - set(ROWS))
    for name in names:
        row = table["rows"][name]
        recall = row["recall@10"]
        ci = f"{recall['point']:.3f} [{recall['lo']:.3f}, {recall['hi']:.3f}]"
        if name == table["reference"]:
            versus = "(reference)"
        else:
            versus = _versus(table["comparisons"][name])
        lines.append(
            f"  {name:<12} {ci:<22} {row['mrr@10']['point']:>7.3f} "
            f"{row['ndcg@10']['point']:>8.3f}   {versus}"
        )
    for pair in table.get("pairs", {}).values():
        lines.append(f"  {pair['system']} vs {pair['baseline']}: {_versus(pair)}")
    sweep = table.get("rrf_k_sensitivity")
    if sweep is not None:
        points = ", ".join(f"{p['k']}: {p['recall@10']:.3f}" for p in sweep["points"])
        lines.append(f"  rrf recall@10 by k ({sweep['note']}): {points}")
    validity = table["validity"]
    if validity["status"] == "current":
        low, high = validity["ci95"]
        lines.append(
            f"  validity {validity['rate']:.1%} [{low:.1%}, {high:.1%}]: every difference is "
            "also quoted divided by it (ADR-0004 §8)"
        )
    else:
        lines.append(f"  validity {validity['status']}: no difference is divided by it")
    checked = table["cross_check"]
    lines.append(
        f"  cross-check: grep {checked['grep']} and bm25_ident {checked['bm25_ident']} hits, "
        "as the verdict found"
    )
    embedding = table.get("embedding")
    if embedding is not None:
        lines.append(
            f"  vectors: {embedding['model']}, fingerprint {embedding['fingerprint'][:12]}, "
            f"as {embedding['source']} records"
        )
    lines.append(f"  wrote {out} and {per_query_out}")
    return "\n".join(lines)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_ablation(
    *,
    golden: Path = DEFAULT_GOLDEN,
    out: Path = DEFAULT_OUT,
    per_query_out: Path = DEFAULT_PER_QUERY,
    verdict: Path = DEFAULT_VERDICT,
    audit: Path = DEFAULT_AUDIT,
    embedding_record: Path = DEFAULT_EMBEDDING_RECORD,
    fixture: Path | None = None,
    model_version: str | None = None,
    repo_id: int | None = None,
) -> int:
    """Score the six rows, run every check, and write both files, or nothing."""
    from provenance.config import settings
    from provenance.eval.generator import pool_version
    from provenance.eval.leakage import PoolIndex, load_pool_from_db
    from provenance.eval.lexical_baselines import Bm25IdentRetriever, GrepRetriever
    from provenance.eval.schema import load_golden_set
    from provenance.graph.db import connection
    from provenance.ingest.pool_embedding import count_missing, fingerprint
    from provenance.retrieve.dense import DenseRetriever
    from provenance.retrieve.embedder import FrozenEmbedder, FrozenEmbeddingMiss
    from provenance.retrieve.fusion import RrfRetriever
    from provenance.retrieve.lexical import Bm25Retriever

    model = model_version or settings.embedding_model_version
    records, _ = runner.select_records(load_golden_set(golden), SPLIT)
    if not records:
        print(f"nothing to score: {golden} has no retained {SPLIT} records", file=sys.stderr)
        return 2
    try:
        if fixture is not None:
            frozen = FrozenEmbedder(fixture, model_id=model)
        else:
            frozen = runner.load_frozen_queries(model)
        check_query_vectors(runner.missing_query_vectors(frozen, records))
        with connection() as conn:
            repo = repo_id if repo_id is not None else runner._repo_id(conn)
            if repo is None:
                print("nothing to score: the corpus is not in `repositories`", file=sys.stderr)
                return 2
            version = pool_version(conn, repo)
            embedding = check_embedding(
                embedding_record,
                model=model,
                fingerprint=fingerprint(conn, repo, model)[0],
                missing=count_missing(conn, repo, model),
            )
            resolved = runner._resolve_gold(conn, records, repo)
            runner._fail_on_unresolved(resolved, allow_unresolved=False)
            pool = load_pool_from_db(conn, repo)
            conn.rollback()
    except (AblationCheckFailed, FrozenEmbeddingMiss) as exc:
        print(f"ABLATION CHECK FAILED: {exc}. Nothing was written.", file=sys.stderr)
        return 1

    def score(retriever: runner.Retriever) -> list[dict[str, Any]]:
        return runner._score_records(
            records,
            resolved,
            retriever,
            frozen,
            k=K,
            rid="ablation",
            model_id=model,
            index_version=version,
        )

    pool_size = len(pool)
    index = PoolIndex(pool)
    probe_rows: tuple[runner.Retriever, ...] = (GrepRetriever(index), Bm25IdentRetriever(index))
    rows = {retriever.name: score(retriever) for retriever in probe_rows}
    # The probe's index holds the whole pool in memory; every later row reads the database.
    del index, pool, probe_rows
    gc.collect()
    try:
        checked = cross_check(
            verdict,
            {name: hits_at_k(rows[name]) for name in CHECKED},
            records=len(records),
            pool_size=pool_size,
        )
    except AblationCheckFailed as exc:
        print(f"ABLATION CHECK FAILED: {exc}. Nothing was written.", file=sys.stderr)
        return 1
    lexical = Bm25Retriever(repo)
    dense = DenseRetriever(repo, model_version=model)
    later: tuple[runner.Retriever, ...] = (
        Bm25Retriever(repo, identifiers="phrase"),
        lexical,
        dense,
        RrfRetriever(lexical, dense),
    )
    rows.update({retriever.name: score(retriever) for retriever in later})
    # Each fused list here reads the two sources' kept top 100s: no query reaches the database.
    sensitivity = [
        sensitivity_point(k, score(RrfRetriever(lexical, dense, k=k))) for k in SENSITIVITY_K
    ]
    commits = sorted({str(r.corpus_commit) for r in records})
    meta = {
        "corpus_commit": commits[0] if len(commits) == 1 else commits,
        "pool_version": version,
        "golden_path": golden.as_posix(),
        "records": len(records),
        "embedding": embedding,
        "fixture_sha256": _sha256(frozen.path),
    }
    table = build_table(
        rows,
        meta=meta,
        validity=read_validity(audit, version),
        checked=checked,
        sensitivity=sensitivity,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_json(table), encoding="utf-8")
    per_query_out.parent.mkdir(parents=True, exist_ok=True)
    per_query_out.write_text("".join(per_query_lines(rows)), encoding="utf-8")
    print(render_summary(table, out, per_query_out))
    return 0
