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
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Final

from provenance.config import settings
from provenance.eval.generator import DEFAULT_OUT as GOLDEN_PATH
from provenance.eval.leakage import LEXICAL_BASELINES
from provenance.eval.metrics import (
    StratifiedProportion,
    StratumCount,
    cohens_kappa,
    stratified_proportion,
    wilson_interval,
)
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

#: ADR-0004 §3's labels, and how much each claims. A final label never claims less than
#: the commit message's, because the text only grows (§4).
RATIONALE: Final = "rationale"
MENTIONS: Final = "mentions"
UNRELATED: Final = "unrelated"
LABELS: Final = (RATIONALE, MENTIONS, UNRELATED)
CLAIMS: Final[Mapping[str, int]] = {RATIONALE: 2, MENTIONS: 1, UNRELATED: 0}
MIN_RELABEL_DAYS: Final = 7

REPORT_SCHEMA: Final = "pace.gold_validity_audit/v1"
REPORT_PATH: Final = Path("results") / "gold_validity_audit.json"
#: ADR-0004 §7: ten times the gate's k.
REACH_K: Final = 100


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


@dataclass(frozen=True)
class LabelRecord:
    """One labelled record: a line of the labels or the relabels file."""

    qid: str
    stratum: str
    message_label: str
    label: str
    note: str | None
    labelled_on: date
    read_change: bool
    read_comments: bool | None

    def __post_init__(self) -> None:
        for value in (self.message_label, self.label):
            if value not in CLAIMS:
                raise AuditError(f"{self.qid}: {value!r} is not one of {', '.join(LABELS)}")
        if CLAIMS[self.label] < CLAIMS[self.message_label]:
            raise AuditError(f"{self.qid}: the final label claims less than the commit message's")
        if self.stratum in PR_STRATA:
            if self.read_comments is None:
                raise AuditError(f"{self.qid}: a PR-linked record says whether comments were read")
        elif self.label != self.message_label or self.read_comments is not None:
            raise AuditError(f"{self.qid}: a commit-only record has one label and no comments")

    def to_json_line(self) -> str:
        raw = {
            "qid": self.qid,
            "stratum": self.stratum,
            "message_label": self.message_label,
            "label": self.label,
            "note": self.note,
            "labelled_on": self.labelled_on.isoformat(),
            "read_change": self.read_change,
            "read_comments": self.read_comments,
        }
        return json.dumps(raw, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> LabelRecord:
        return cls(
            qid=str(raw["qid"]),
            stratum=str(raw["stratum"]),
            message_label=str(raw["message_label"]),
            label=str(raw["label"]),
            note=raw.get("note"),
            labelled_on=date.fromisoformat(raw["labelled_on"]),
            read_change=bool(raw["read_change"]),
            read_comments=raw.get("read_comments"),
        )


def load_labels(path: Path) -> dict[str, LabelRecord]:
    """A labels file by qid, in file order. A qid labelled twice is an error."""
    labels: dict[str, LabelRecord] = {}
    if not path.exists():
        return labels
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = LabelRecord.from_json(json.loads(line))
        if record.qid in labels:
            raise AuditError(f"{path}:{lineno}: {record.qid} is labelled twice")
        labels[record.qid] = record
    return labels


def append_label(path: Path, record: LabelRecord) -> None:
    """One line, appended at once: a stop loses at most the record on screen."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record.to_json_line())


def check_relabel_allowed(sample: Sample, labels: Mapping[str, LabelRecord], today: date) -> None:
    """ADR-0004 §6: every subset record labelled once, and seven days since the last label."""
    subset = sample.relabel_records()
    missing = [r.qid for r in subset if r.qid not in labels]
    if missing:
        raise AuditError(
            f"{len(missing)} of the {len(subset)} re-label records have no first label yet "
            f"(first: {missing[0]}); finish `pace audit label` first"
        )
    last = max(labels[r.qid].labelled_on for r in subset)
    if (today - last).days < MIN_RELABEL_DAYS:
        opens = last + timedelta(days=MIN_RELABEL_DAYS)
        raise AuditError(f"the last first label is from {last}; the re-label opens on {opens}")


def _rate(hits: int, n: int) -> dict[str, Any]:
    low, high = wilson_interval(hits, n)
    return {"rate": round(hits / n, 4), "ci95": [round(low, 4), round(high, 4)]}


def _estimate(p: StratifiedProportion) -> dict[str, Any]:
    return {
        "rate": round(p.estimate, 4),
        "ci95": [round(p.low, 4), round(p.high, 4)],
        "effective_n": round(p.effective_n, 2),
    }


def label_section(sample: Sample, labels: Mapping[str, LabelRecord]) -> dict[str, Any]:
    """ADR-0004 §5's estimates, once every sampled record is labelled.

    A partial labelling gives no rate: it would break the strata sizes the weights assume.
    """
    got = {r.qid: labels[r.qid] for r in sample.records if r.qid in labels}
    if len(got) < len(sample.records):
        return {"status": "incomplete", "labelled": len(got), "of": len(sample.records)}
    for sampled in sample.records:
        if got[sampled.qid].stratum != sampled.stratum:
            raise AuditError(
                f"{sampled.qid}: labelled as {got[sampled.qid].stratum}, "
                f"sampled as {sampled.stratum}"
            )
    strata = sorted(sample.strata)
    rows = {s: [got[r.qid] for r in sample.records if r.stratum == s] for s in strata}

    def count(stratum: str, value: str, *, message: bool = False) -> StratumCount:
        size, sampled = sample.strata[stratum]
        hits = sum(1 for r in rows[stratum] if (r.message_label if message else r.label) == value)
        return StratumCount(size, sampled, hits)

    def share(value: str) -> float:
        return round(stratified_proportion([count(s, value) for s in strata]).estimate, 4)

    by_stratum: dict[str, Any] = {}
    for s in strata:
        final = Counter(r.label for r in rows[s])
        first = Counter(r.message_label for r in rows[s])
        n = len(rows[s])
        by_stratum[s] = {
            "population": sample.strata[s][0],
            "sampled": n,
            "labels": {lab: final[lab] for lab in LABELS},
            "validity": _rate(final[RATIONALE], n),
            "message_only": {
                "labels": {lab: first[lab] for lab in LABELS},
                "validity": _rate(first[RATIONALE], n),
            },
        }
    linked = [r for s in PR_STRATA for r in rows[s]]
    message_only = stratified_proportion([count(s, RATIONALE, message=True) for s in PR_STRATA])
    with_pr_text = stratified_proportion([count(s, RATIONALE) for s in PR_STRATA])
    return {
        "status": "complete",
        "labelled": len(got),
        "of": len(sample.records),
        "by_stratum": by_stratum,
        "overall": {
            **_estimate(stratified_proportion([count(s, RATIONALE) for s in strata])),
            "mentions_share": share(MENTIONS),
            "unrelated_share": share(UNRELATED),
        },
        "pr_text": {
            "records": len(linked),
            "message_only": _estimate(message_only),
            "with_pr_text": _estimate(with_pr_text),
            "supplied_by_pr_text": sum(
                1 for r in linked if r.message_label != RATIONALE and r.label == RATIONALE
            ),
        },
        "reading": {
            "change_opened": sum(1 for r in got.values() if r.read_change),
            "pr_text_shown": sum(1 for r in linked if r.message_label != RATIONALE),
            "comments_opened": sum(1 for r in linked if r.read_comments),
        },
    }


def _pick(record: LabelRecord, message: bool) -> str:
    return record.message_label if message else record.label


def agreement_section(
    sample: Sample, labels: Mapping[str, LabelRecord], relabels: Mapping[str, LabelRecord]
) -> dict[str, Any]:
    """ADR-0004 §6: kappa and observed agreement, once every subset record is re-labelled."""
    subset = sample.relabel_records()
    if any(r.qid not in labels or r.qid not in relabels for r in subset):
        done = sum(1 for r in subset if r.qid in relabels)
        return {"status": "not_measured", "relabelled": done, "of": len(subset)}
    section: dict[str, Any] = {"status": "measured", "n": len(subset)}
    for name, message in (("final", False), ("message_only", True)):
        first = [_pick(labels[r.qid], message) for r in subset]
        second = [_pick(relabels[r.qid], message) for r in subset]
        kappa = cohens_kappa(first, second)
        observed = sum(a == b for a, b in zip(first, second, strict=True)) / len(first)
        entry: dict[str, Any] = {
            "observed": round(observed, 4),
            "kappa": None if kappa is None else round(kappa, 4),
        }
        if kappa is None:
            entry["kappa_undefined"] = "every label the same in both passes"
        section[name] = entry
    return section


def reachability_section(
    sample: Sample, reached: Mapping[str, bool], strata: Mapping[str, str]
) -> dict[str, Any]:
    """ADR-0004 §7: dev records whose gold no lexical baseline ranks in its top REACH_K."""

    def summary(qids: Sequence[str]) -> dict[str, Any]:
        unreachable = sum(1 for qid in qids if not reached[qid])
        share = round(unreachable / len(qids), 4) if qids else None
        return {"records": len(qids), "unreachable": unreachable, "share": share}

    return {
        "k": REACH_K,
        "baselines": list(LEXICAL_BASELINES),
        "dense": "not measured: no frozen vectors",
        **summary(sorted(reached)),
        "by_stratum": {
            s: summary(sorted(q for q, st in strata.items() if st == s))
            for s in sorted(sample.strata)
        },
    }


def build_report(
    sample: Sample,
    labels: Mapping[str, LabelRecord],
    relabels: Mapping[str, LabelRecord],
    *,
    reachability: Mapping[str, Any],
    linkage_bias: Mapping[str, Any],
) -> dict[str, Any]:
    """`results/gold_validity_audit.json`, from the committed files and the database."""
    return {
        "schema": REPORT_SCHEMA,
        "sample": {
            "pool_version": sample.pool_version,
            "corpus_commit": sample.corpus_commit,
            "seeds": dict(SEEDS),
            "strata": {
                s: {"population": n, "sampled": k} for s, (n, k) in sorted(sample.strata.items())
            },
        },
        "labels": label_section(sample, labels),
        "self_agreement": agreement_section(sample, labels, relabels),
        "reachability": dict(reachability),
        "linkage_bias": dict(linkage_bias),
    }


def render_report_json(report: Mapping[str, Any]) -> str:
    """Sorted keys and no timestamps: the same files and database give the same bytes."""
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def render_report_summary(report: Mapping[str, Any], out: Path) -> str:
    labels, agreement = report["labels"], report["self_agreement"]
    reach, bias = report["reachability"], report["linkage_bias"]
    if labels["status"] == "complete":
        overall = labels["overall"]
        low, high = overall["ci95"]
        validity = f"validity {overall['rate']:.1%} [{low:.1%}, {high:.1%}] over the dev set"
    else:
        validity = f"labels incomplete: {labels['labelled']} of {labels['of']}"
    if agreement["status"] == "measured":
        kappa = agreement["final"]["kappa"]
        kappa_text = "undefined" if kappa is None else f"{kappa:.2f}"
        agreed = (
            f"self-agreement: kappa {kappa_text}, observed "
            f"{agreement['final']['observed']:.0%} (n = {agreement['n']})"
        )
    else:
        agreed = (
            f"self-agreement: not measured ({agreement['relabelled']} of "
            f"{agreement['of']} re-labelled)"
        )
    groups = bias["groups"]
    return "\n".join(
        [
            f"gold-validity audit on {report['sample']['pool_version']}",
            f"  {validity}",
            f"  {agreed}",
            f"  reachability: {reach['unreachable']:,} of {reach['records']:,} dev records "
            f"({reach['share'] or 0.0:.1%}) beyond every lexical baseline's top {reach['k']}",
            f"  linkage bias: {groups['linked']:,} linked against {groups['no_reference']:,} "
            f"unlinked commits; distance by module {bias['module_tvd']}, "
            f"by year {bias['year_tvd']}",
            f"  wrote {out}",
        ]
    )


def report_main(argv: Sequence[str] | None = None) -> int:
    """`pace audit report`: the validity rate, the self-agreement and both companions."""
    parser = argparse.ArgumentParser(
        prog="pace audit report",
        description="Turn the gold-validity labels into ADR-0004's report.",
    )
    parser.add_argument("--out", default=str(REPORT_PATH), help=f"default: {REPORT_PATH}")
    args = parser.parse_args(list(argv) if argv is not None else None)

    from provenance.analysis.linkage_bias import NothingToCompare, linkage_bias
    from provenance.eval.generator import pool_version
    from provenance.eval.leakage import (
        PoolIndex,
        load_pool_from_db,
        reachable_at_k,
        resolve_gold_from_db,
    )
    from provenance.graph.db import connection
    from provenance.graph.repos import UnknownRepository, resolve_repo_id

    try:
        sample = load_sample(SAMPLE_PATH)
        records = load_golden_set(GOLDEN_PATH)
        labels, relabels = load_labels(LABELS_PATH), load_labels(RELABELS_PATH)
        with connection() as conn:
            repo_id = resolve_repo_id(conn, settings.repo_owner, settings.repo_name)
            check_fresh(sample, records, pool_version(conn, repo_id))
            dev = population(records)
            index = PoolIndex(load_pool_from_db(conn, repo_id))
            gold = {r.qid: resolve_gold_from_db(r, conn, repo_id).chunk_ids for r in dev}
            reached = reachable_at_k(index, dev, gold, REACH_K)
            bias = linkage_bias(conn, repo_id)
    except (UnknownRepository, NothingToCompare) as exc:
        print(f"nothing to report: {exc}", file=sys.stderr)
        return 2
    except AuditError as exc:
        print(f"AUDIT REFUSED: {exc}", file=sys.stderr)
        return 1
    reachability = reachability_section(sample, reached, {r.qid: stratum_of(r) for r in dev})
    report = build_report(sample, labels, relabels, reachability=reachability, linkage_bias=bias)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report_json(report), encoding="utf-8")
    print(render_report_summary(report, out))
    return 0


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
