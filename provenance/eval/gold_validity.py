"""ADR-0004: how much of the benchmark's gold explains its function.

`pace audit sample` draws ADR-0004 §2's stratified sample, once. `pace audit report` turns
the author's labels into the validity rate and the self-agreement, beside two companions
that need no labeller. The labelling session itself is `provenance.eval.labelling`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from provenance.config import settings
from provenance.eval.generator import DEFAULT_OUT as GOLDEN_PATH
from provenance.eval.schema import GoldenRecord, load_golden_set, passes_leakage_filter

SAMPLE_SCHEMA: Final = "pace.gold_validity_sample/v1"
AUDIT_DIR: Final = Path("provenance") / "eval" / "audit"
SAMPLE_PATH: Final = AUDIT_DIR / "gold_validity_sample.json"
LABELS_PATH: Final = AUDIT_DIR / "gold_validity_labels.jsonl"
RELABELS_PATH: Final = AUDIT_DIR / "gold_validity_relabels.jsonl"

#: ADR-0004 §2's strata and sizes, fixed before the sample was drawn.
PROVENANCE_PR: Final = "provenance-pr"
ARCHAEOLOGY_PR: Final = "archaeology-pr"
PROVENANCE_COMMIT: Final = "provenance-commit"
ARCHAEOLOGY_COMMIT: Final = "archaeology-commit"
ALLOCATION: Final[Mapping[str, int]] = {
    PROVENANCE_PR: 11,
    ARCHAEOLOGY_PR: 33,
    PROVENANCE_COMMIT: 6,
    ARCHAEOLOGY_COMMIT: 6,
}
PR_STRATA: Final = (PROVENANCE_PR, ARCHAEOLOGY_PR)
RELABEL_SIZE: Final = 15
SEEDS: Final[Mapping[str, str]] = {
    "sample": "gold-validity-v1",
    "order": "gold-validity-order-v1",
    "relabel": "gold-validity-relabel-v1",
    "relabel_order": "gold-validity-relabel-order-v1",
}


class AuditError(RuntimeError):
    """The audit cannot go on; the message says why."""


class SampleExists(AuditError):
    """A sample is a pre-registration: it is drawn once (ADR-0004 §2)."""


class AuditStale(AuditError):
    """The golden set or the pool changed since the sample was drawn (ADR-0004 §9)."""


def stratum_of(record: GoldenRecord) -> str:
    """`provenance-pr`, `archaeology-commit`, ...: query class x whether a PR is linked."""
    linked = "pr" if record.required_evidence.pr_numbers else "commit"
    return f"{record.query_class}-{linked}"


def population(records: Iterable[GoldenRecord]) -> list[GoldenRecord]:
    """Retained, generated dev records: what ADR-0004 §2 samples."""
    return [
        r for r in records if r.split == "dev" and not r.is_handwritten and passes_leakage_filter(r)
    ]


def record_hash(record: GoldenRecord) -> str:
    """A hash of what the audit reads of a record: question, split, class, evidence, anchor."""
    required = record.required_evidence.keys()
    anchors = record.supporting_evidence.keys()
    payload = {
        "qid": record.qid,
        "question": record.question,
        "split": record.split,
        "query_class": str(record.query_class),
        "required": [list(key) for key in required],
        "anchors": [list(key) for key in anchors],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _rank(seed: str, qid: str) -> int:
    """sha256("seed:qid") as an integer: ADR-0003 §7's rule, so a draw depends on nothing else."""
    return int.from_bytes(hashlib.sha256(f"{seed}:{qid}".encode()).digest(), "big")


@dataclass(frozen=True)
class SampledRecord:
    qid: str
    stratum: str
    order: int
    relabel_order: int | None
    record_sha256: str


@dataclass(frozen=True)
class Sample:
    """ADR-0004 §2's pre-registration: the sampled records, in presentation order."""

    pool_version: str
    corpus_commit: str
    #: stratum -> (records in the population, records sampled)
    strata: Mapping[str, tuple[int, int]]
    records: tuple[SampledRecord, ...]

    def relabel_records(self) -> list[SampledRecord]:
        """The re-label subset, in its own order."""
        chosen = [r for r in self.records if r.relabel_order is not None]
        return sorted(chosen, key=lambda r: r.relabel_order or 0)

    def to_json(self) -> str:
        doc = {
            "schema": SAMPLE_SCHEMA,
            "golden_path": GOLDEN_PATH.as_posix(),
            "pool_version": self.pool_version,
            "corpus_commit": self.corpus_commit,
            "seeds": dict(SEEDS),
            "strata": {s: {"population": n, "sampled": k} for s, (n, k) in self.strata.items()},
            "records": [
                {
                    "qid": r.qid,
                    "stratum": r.stratum,
                    "order": r.order,
                    "relabel_order": r.relabel_order,
                    "record_sha256": r.record_sha256,
                }
                for r in self.records
            ],
        }
        return json.dumps(doc, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Sample:
        doc = json.loads(text)
        if doc.get("schema") != SAMPLE_SCHEMA:
            raise AuditError(f"not a gold-validity sample (schema {doc.get('schema')!r})")
        rows = sorted(doc["records"], key=lambda r: int(r["order"]))
        return cls(
            pool_version=doc["pool_version"],
            corpus_commit=doc["corpus_commit"],
            strata={s: (int(v["population"]), int(v["sampled"])) for s, v in doc["strata"].items()},
            records=tuple(
                SampledRecord(
                    r["qid"], r["stratum"], int(r["order"]), r["relabel_order"], r["record_sha256"]
                )
                for r in rows
            ),
        )


def draw_sample(
    records: Sequence[GoldenRecord],
    *,
    pool_version: str,
    corpus_commit: str,
    allocation: Mapping[str, int] = ALLOCATION,
    relabel_size: int = RELABEL_SIZE,
) -> Sample:
    """ADR-0004 §2's sample: the same golden set always gives the same records and order."""
    members: dict[str, list[GoldenRecord]] = {stratum: [] for stratum in allocation}
    for record in population(records):
        stratum = stratum_of(record)
        if stratum in members:
            members[stratum].append(record)
    chosen: list[tuple[str, GoldenRecord]] = []
    for stratum, size in allocation.items():
        if len(members[stratum]) < size:
            raise AuditError(
                f"stratum {stratum} holds {len(members[stratum])} records, fewer than its {size}"
            )
        ranked = sorted(members[stratum], key=lambda r: _rank(SEEDS["sample"], r.qid))
        chosen += [(stratum, record) for record in ranked[:size]]
    ordered = sorted(chosen, key=lambda pair: _rank(SEEDS["order"], pair[1].qid))
    subset = sorted(ordered, key=lambda pair: _rank(SEEDS["relabel"], pair[1].qid))
    relabel_qids = sorted(
        (record.qid for _, record in subset[:relabel_size]),
        key=lambda qid: _rank(SEEDS["relabel_order"], qid),
    )
    relabel_order = {qid: i for i, qid in enumerate(relabel_qids, 1)}
    return Sample(
        pool_version=pool_version,
        corpus_commit=corpus_commit,
        strata={s: (len(members[s]), size) for s, size in allocation.items()},
        records=tuple(
            SampledRecord(r.qid, s, i, relabel_order.get(r.qid), record_hash(r))
            for i, (s, r) in enumerate(ordered, 1)
        ),
    )


def write_sample(sample: Sample, path: Path) -> None:
    if path.exists():
        raise SampleExists(
            f"{path} exists: the sample is a pre-registration and is drawn once (ADR-0004 §2)"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sample.to_json(), encoding="utf-8")


def load_sample(path: Path) -> Sample:
    if not path.exists():
        raise AuditError(f"{path} does not exist: run `pace audit sample` first")
    return Sample.from_json(path.read_text(encoding="utf-8"))


def check_fresh(
    sample: Sample, records: Sequence[GoldenRecord], pool_version: str
) -> dict[str, GoldenRecord]:
    """The sampled records as stored now, or AuditStale when the audit no longer describes them."""
    if pool_version != sample.pool_version:
        raise AuditStale(
            f"the pool is {pool_version}, but the sample was drawn on {sample.pool_version}: "
            "draw a new sample (ADR-0004 §9)"
        )
    by_qid = {r.qid: r for r in records}
    fresh: dict[str, GoldenRecord] = {}
    for sampled in sample.records:
        record = by_qid.get(sampled.qid)
        if record is None:
            raise AuditStale(f"{sampled.qid} is no longer in the golden set (ADR-0004 §9)")
        if record_hash(record) != sampled.record_sha256:
            raise AuditStale(f"{sampled.qid} changed since the sample was drawn (ADR-0004 §9)")
        fresh[sampled.qid] = record
    return fresh


def corpus_commit_of(records: Sequence[GoldenRecord], pool_version: str) -> str:
    """The golden set's corpus commit, once it is shown to have been measured on this pool."""
    sampled = population(records)
    measured = sorted({str(r.leakage.probe_index_version) for r in sampled if r.leakage})
    if measured != [pool_version]:
        raise AuditError(
            f"the golden set was measured on {measured}, but the pool is {pool_version}: "
            "run `pace golden generate` first"
        )
    commits = sorted({str(r.corpus_commit) for r in sampled})
    if len(commits) != 1:
        raise AuditError(f"the golden set names {len(commits)} corpus commits, not one")
    return commits[0]


def render_sample(sample: Sample, path: Path) -> str:
    relabel = sum(1 for r in sample.records if r.relabel_order is not None)
    lines = [
        f"gold-validity sample on {sample.pool_version}: "
        f"{len(sample.records)} records, {relabel} of them to re-label"
    ]
    for stratum, (size, sampled) in sorted(sample.strata.items()):
        lines.append(f"  {stratum:<20} {sampled:>3} of {size:,}")
    lines.append(f"  wrote {path}; commit it before the first label")
    return "\n".join(lines)


def sample_main(argv: Sequence[str] | None = None) -> int:
    """`pace audit sample`: draw ADR-0004 §2's sample, once."""
    parser = argparse.ArgumentParser(
        prog="pace audit sample",
        description="Draw the gold-validity sample once (ADR-0004 §2).",
    )
    parser.add_argument("--golden", default=str(GOLDEN_PATH), help=f"default: {GOLDEN_PATH}")
    parser.add_argument("--out", default=str(SAMPLE_PATH), help=f"default: {SAMPLE_PATH}")
    args = parser.parse_args(list(argv) if argv is not None else None)

    from provenance.eval.generator import pool_version
    from provenance.graph.db import connection
    from provenance.graph.repos import UnknownRepository, resolve_repo_id

    out = Path(args.out)
    try:
        records = load_golden_set(args.golden)
        with connection() as conn:
            repo_id = resolve_repo_id(conn, settings.repo_owner, settings.repo_name)
            version = pool_version(conn, repo_id)
        sample = draw_sample(
            records, pool_version=version, corpus_commit=corpus_commit_of(records, version)
        )
        write_sample(sample, out)
    except UnknownRepository as exc:
        print(f"nothing to sample: {exc}", file=sys.stderr)
        return 2
    except AuditError as exc:
        print(f"AUDIT REFUSED: {exc}", file=sys.stderr)
        return 1
    print(render_sample(sample, out))
    return 0
