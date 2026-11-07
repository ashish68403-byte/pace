"""Slice B5a: the ablation table's lexical rows, through the runner's own scoring.

`pace eval ablation` scores three retrievers on the retained dev records -- grep, the
leakage probe's identifier-aware BM25 (`bm25_ident`, the reference every lift is quoted
against, ADR-0003 §6) and the production BM25 index (`bm25`) -- and writes
`results/ablation.json` and `results/ablation_per_query.jsonl`. Every row goes through
`runner._score_records` and `runner.aggregate`, so the CI check and this table share one
definition of recall. Each difference against the reference is quoted twice, raw and
divided by the gold's validity (ADR-0004 §8). Before anything is written, grep's and
bm25_ident's hits must equal the leakage verdict's: two computations of one number.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from provenance.eval import runner
from provenance.eval.metrics import discordant, mde, paired_bootstrap

SCHEMA: Final = "pace.ablation/v1"
K: Final = 10
SPLIT: Final = "dev"
REFERENCE: Final = "bm25_ident"
#: The rows the leakage verdict also measured, which the cross-check compares.
CHECKED: Final = ("grep", "bm25_ident")
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


def build_table(
    rows: Rows,
    *,
    meta: Mapping[str, Any],
    validity: Mapping[str, Any],
    checked: Mapping[str, Any],
) -> dict[str, Any]:
    """`results/ablation.json`: every row, and every comparison with the reference."""
    if REFERENCE not in rows:
        raise ValueError(f"the reference row {REFERENCE} is missing")
    return {
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
        "validity": dict(validity),
        "cross_check": dict(checked),
    }


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


def render_summary(table: Mapping[str, Any], out: Path, per_query_out: Path) -> str:
    lines = [
        f"ablation on {table['pool_version']}: {table['records']:,} retained {table['split']} "
        f"records, k = {table['k']}, reference {table['reference']}",
        f"  {'row':<12} {'recall@10 [95% CI]':<22} {'MRR@10':>7} {'nDCG@10':>8}   versus reference",
    ]
    for name, row in table["rows"].items():
        recall = row["recall@10"]
        ci = f"{recall['point']:.3f} [{recall['lo']:.3f}, {recall['hi']:.3f}]"
        if name == table["reference"]:
            versus = "(reference)"
        else:
            c = table["comparisons"][name]
            mde_text = "n/a" if c["mde"] is None else f"{c['mde']:.3f}"
            versus = (
                f"{c['delta']:+.3f} [{c['lo']:+.3f}, {c['hi']:+.3f}], "
                f"D={c['discordant']}, MDE={mde_text}"
            )
        lines.append(
            f"  {name:<12} {ci:<22} {row['mrr@10']['point']:>7.3f} "
            f"{row['ndcg@10']['point']:>8.3f}   {versus}"
        )
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
    lines.append(f"  wrote {out} and {per_query_out}")
    return "\n".join(lines)
