"""Slice B3's golden set: every candidate question, measured for leakage, and its census.

`pace golden generate` turns the in-scope history's symbol events (`eval.candidates`)
into `GoldenRecord`s, measures each one's leakage against the commit-message pool with
the verdict's own code (`eval.leakage`), and writes two files
(docs/tasks/eval-harness/003_golden_set_generator.md):

* `provenance/eval/golden/auto_full.jsonl` -- every candidate, sorted by qid, with its
  leakage facts; `passes_leakage_filter` says which are retained (ADR-0003 §5);
* `results/golden_census.json` -- counts and identifiers only: what each rule removed,
  what the leakage filter removed, and the split.

Nothing is sampled (ADR-0003 §5), so the retention rate is a census figure. The same
graph, pool and corpus always give the same bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from provenance.config import settings
from provenance.eval.candidates import (
    ADDED,
    CHANGED,
    Event,
    SymbolWalk,
    apply_rules,
    walk_symbols,
)
from provenance.eval.leakage import (
    LeakageFacts,
    PoolIndex,
    load_pool_from_db,
    resolve_gold_from_db,
)
from provenance.eval.schema import (
    GoldenRecord,
    GoldEvidence,
    LeakageProbe,
    QueryClass,
    Split,
    SymbolAnchor,
    assign_splits,
    dev_share_by_class,
    group_key,
    passes_leakage_filter,
    write_golden_set,
)
from provenance.graph.tables import CHUNK_EVIDENCE, COMMITS
from provenance.ingest.message_pool import MESSAGE_STRATEGY_VERSION

GENERATION_STRATEGY: Final = "ast_diff_v0"

#: Each template's query class (ADR-0003 §3).
QUERY_CLASS: Final[Mapping[str, QueryClass]] = {
    ADDED: QueryClass.PROVENANCE,
    CHANGED: QueryClass.ARCHAEOLOGY,
}
TEMPLATES: Final[Mapping[str, str]] = {
    ADDED: "Why was `X` added to `path`?",
    CHANGED: "Why did `X` change in <Month YYYY>?",
}

#: Why ADR-0003 §5 drops a generated record, in the order the filter checks.
RANK_1: Final = "grep_answers_at_rank_1"
RARE_TOKEN: Final = "rare_token_shared_with_gold"
UNMEASURED: Final = "unmeasured"
DROP_REASONS: Final = (RANK_1, RARE_TOKEN, UNMEASURED)

#: English month names, fixed: `strftime("%B")` follows the machine's locale.
_MONTHS: Final = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

DEFAULT_OUT: Final = Path("provenance") / "eval" / "golden" / "auto_full.jsonl"
DEFAULT_CENSUS: Final = Path("results") / "golden_census.json"
CENSUS_SCHEMA: Final = "pace.golden_census/v1"
#: Dropped qids listed per reason in the census: enough to inspect, not a second copy.
DROPPED_EXAMPLES: Final = 20

_HEAD_SQL: Final = (
    f"SELECT commit_sha FROM {COMMITS} WHERE repo_id = %s ORDER BY topo_order DESC LIMIT 1"
)
#: Slice B2's PR links: the commit a document is (`self`) beside the PR it belongs to.
_LINKED_PRS_SQL: Final = f"""
    SELECT s.evidence_key, l.evidence_key
    FROM {CHUNK_EVIDENCE} s
    JOIN {CHUNK_EVIDENCE} l ON l.repo_id = s.repo_id AND l.chunk_id = s.chunk_id
    WHERE s.repo_id = %s
      AND s.source = 'self' AND s.evidence_kind = 'commit'
      AND l.source = 'linked' AND l.evidence_kind = 'pull_request'
"""


class NothingToGenerate(RuntimeError):
    """No graph, or no pool, to generate from."""


class GenerationInvariantError(RuntimeError):
    """The graph, the corpus and the pool disagree. Nothing was written."""


@dataclass(frozen=True)
class Generation:
    records: list[GoldenRecord]
    census: dict[str, Any]


def month_label(month: str) -> str:
    """`2023-03` -> `March 2023`."""
    year, number = month.split("-")
    return f"{_MONTHS[int(number) - 1]} {year}"


def question_for(event: Event) -> str:
    """The question ADR-0003 §3's template asks about this event."""
    if event.template == ADDED:
        return f"Why was `{event.qualified_name}` added to `{event.path}`?"
    return f"Why did `{event.qualified_name}` change in {month_label(event.month)}?"


def qid_for(event: Event) -> str:
    """`<template>-<sha[:12]>-<sha256(path::X)[:8]>`: the same candidate, the same id."""
    anchor = hashlib.sha256(f"{event.path}::{event.qualified_name}".encode()).hexdigest()
    return f"{event.template}-{event.sha[:12]}-{anchor[:8]}"


def required_evidence_for(event: Event, pr: int | None) -> GoldEvidence:
    """The commit, plus its PR when slice B2 linked one (ADR-0003 §4)."""
    return GoldEvidence(commit_shas=[event.sha], pr_numbers=[] if pr is None else [pr])


def record_for(
    event: Event,
    *,
    pr: int | None,
    split: Split,
    facts: LeakageFacts,
    corpus_commit: str,
    index_version: str,
) -> GoldenRecord:
    """One candidate as a golden record, carrying the leakage facts measured for it."""
    qid = qid_for(event)
    if facts.gold_symbol_df is None:
        raise ValueError(f"{qid}: a generated record needs its symbol's document frequency")
    return GoldenRecord(
        qid=qid,
        query_class=QUERY_CLASS[event.template],
        split=split,
        question=question_for(event),
        required_evidence=required_evidence_for(event, pr),
        supporting_evidence=GoldEvidence(
            symbol_anchors=[SymbolAnchor(path=event.path, qualified_name=event.qualified_name)]
        ),
        generation_strategy=GENERATION_STRATEGY,
        leakage=LeakageProbe(
            baseline_grep_r10=float(facts.grep_hit_at_k),
            baseline_bm25_r10=float(facts.bm25_hit_at_k),
            gold_symbol_df=facts.gold_symbol_df,
            grep_answers_at_rank_1=facts.grep_answers_at_rank_1,
            rare_tokens_shared_with_gold=facts.rare_tokens_shared_with_gold,
            probe_index_version=index_version,
            probe_corpus_commit=corpus_commit,
        ),
        corpus_commit=corpus_commit,
    )


def drop_reason(record: GoldenRecord) -> str | None:
    """Why ADR-0003 §5 drops this record, or None when it is retained.

    Defined through `passes_leakage_filter`, so the two cannot disagree. A dropped record
    gets one reason, checked in the filter's order.
    """
    if passes_leakage_filter(record):
        return None
    probe = record.leakage
    if (
        probe is None
        or probe.grep_answers_at_rank_1 is None
        or probe.rare_tokens_shared_with_gold is None
    ):
        return UNMEASURED
    return RANK_1 if probe.grep_answers_at_rank_1 else RARE_TOKEN


def generate(conn: Any, repo_id: int, corpus: Path) -> Generation:
    """Every candidate as a measured record, sorted by qid, and the census. Reads only."""
    head = conn.execute(_HEAD_SQL, (repo_id,)).fetchone()
    if head is None:
        raise NothingToGenerate(f"repo {repo_id} has no commits; run `pace ingest` first")
    corpus_commit = str(head[0])
    checked_out = _git_head(corpus)
    if checked_out != corpus_commit:
        raise GenerationInvariantError(
            f"the corpus at {corpus} is at {checked_out[:10]} but the graph ends at "
            f"{corpus_commit[:10]}: questions would name code the graph never saw"
        )
    pool = load_pool_from_db(conn, repo_id)
    if not pool:
        raise NothingToGenerate(f"repo {repo_id} has no pool documents; run `pace pool build`")

    walk = walk_symbols(conn, repo_id, corpus)
    candidates, rules = apply_rules(walk)
    links = conn.execute(_LINKED_PRS_SQL, (repo_id,)).fetchall()
    prs = {str(sha): int(pr) for sha, pr in links}
    splits = assign_splits(group_key(e.sha, prs.get(e.sha)) for e in candidates)
    index = PoolIndex(pool)
    index_version = f"{MESSAGE_STRATEGY_VERSION}:{len(pool)}"

    records: list[GoldenRecord] = []
    for event in candidates:
        pr = prs.get(event.sha)
        gold = {c for c in _gold_documents(conn, repo_id, event, pr) if c in index.by_chunk}
        if not gold:
            raise GenerationInvariantError(
                f"{qid_for(event)}: commit {event.sha[:10]} has no document in the pool; "
                "rebuild it with `pace pool build`"
            )
        records.append(
            record_for(
                event,
                pr=pr,
                split=splits[group_key(event.sha, pr)],
                facts=index.measure(question_for(event), gold, event.short_name),
                corpus_commit=corpus_commit,
                index_version=index_version,
            )
        )
    records.sort(key=lambda record: record.qid)
    repeated = [qid for qid, n in Counter(r.qid for r in records).items() if n > 1]
    if repeated:
        raise GenerationInvariantError(f"{len(repeated)} qids repeat (first: {repeated[0]})")
    census = build_census(
        corpus_commit=corpus_commit,
        index_version=index_version,
        walk=walk,
        rules=rules,
        records=records,
    )
    return Generation(records=records, census=census)


def _gold_documents(conn: Any, repo_id: int, event: Event, pr: int | None) -> set[str]:
    """The documents that answer this candidate, found by the verdict's own resolver.

    The verdict recomputes every stored fact against the pool (ADR-0003 §6), so
    generation finds gold exactly as the verdict will: through `resolve_gold_from_db`.
    The lookup record is built unvalidated, because the leakage facts the schema
    requires of a generated record are what this lookup is for.
    """
    lookup = GoldenRecord.model_construct(
        qid=qid_for(event),
        query_class=QUERY_CLASS[event.template],
        required_evidence=required_evidence_for(event, pr),
    )
    return resolve_gold_from_db(lookup, conn, repo_id).chunk_ids


def _git_head(corpus: Path) -> str:
    done = subprocess.run(
        ["git", "-C", str(corpus), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def build_census(
    *,
    corpus_commit: str,
    index_version: str,
    walk: SymbolWalk,
    rules: dict[str, dict[str, int]],
    records: Sequence[GoldenRecord],
) -> dict[str, Any]:
    """Counts and identifiers only, so the file can be committed (spec, Records)."""
    reasons = {record.qid: drop_reason(record) for record in records}
    retained = [record for record in records if reasons[record.qid] is None]

    def outcomes(group: Sequence[GoldenRecord]) -> dict[str, int]:
        counted = Counter(reasons[record.qid] for record in group)
        return {
            "candidates": len(group),
            "retained": counted[None],
            **{reason: counted[reason] for reason in DROP_REASONS},
        }

    by_template = {
        template: [r for r in records if r.query_class == QUERY_CLASS[template]]
        for template in (ADDED, CHANGED)
    }
    zero_df = sum(1 for record in retained if record.gold_symbol_df == 0)
    return {
        "schema": CENSUS_SCHEMA,
        "corpus_commit": corpus_commit,
        "generation_strategy": GENERATION_STRATEGY,
        "probe_index_version": index_version,
        "templates": {
            template: {"query_class": str(QUERY_CLASS[template]), "question": question}
            for template, question in TEMPLATES.items()
        },
        "walk": {
            "revisions": walk.revisions,
            "diffed": walk.diffed,
            "skipped_unknown_side": walk.skipped_unknown_side,
            "unparseable_blobs": walk.unparseable_blobs,
        },
        "rules": rules,
        "retention": {
            **{template: outcomes(group) for template, group in by_template.items()},
            "all": outcomes(records),
        },
        "retention_rate": _share(len(retained), len(records)),
        "retained_by_split": {
            split: sum(1 for record in retained if record.split == split)
            for split in ("dev", "test")
        },
        "dev_share_by_class": {
            cls: round(share, 4) for cls, share in sorted(dev_share_by_class(retained).items())
        },
        "retained_with_pr": sum(1 for record in retained if record.required_evidence.pr_numbers),
        "retained_gold_symbol_df_zero_share": _share(zero_df, len(retained)),
        "dropped_examples": {
            reason: [qid for qid, why in reasons.items() if why == reason][:DROPPED_EXAMPLES]
            for reason in DROP_REASONS
        },
    }


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def render_census(census: dict[str, Any]) -> str:
    """Sorted keys and no timestamps: the same inputs always render the same bytes."""
    return json.dumps(census, indent=2, sort_keys=True) + "\n"


def _header(census: dict[str, Any]) -> str:
    every = census["retention"]["all"]
    return "\n".join(
        [
            f"PACE golden set: {every['candidates']:,} questions from `pace golden generate` "
            f"({GENERATION_STRATEGY}).",
            "Every candidate ADR-0003 §3 allows, with its leakage facts; passes_leakage_filter()",
            f"retains {every['retained']:,} of them (ADR-0003 §5). Corpus "
            f"{census['corpus_commit']}, pool {census['probe_index_version']}.",
            "Generated, not curated: regenerate rather than edit. Census: "
            "results/golden_census.json.",
        ]
    )


def write_outputs(generation: Generation, out: Path, census_path: Path) -> None:
    """The golden set and its census, as `main` writes them."""
    write_golden_set(generation.records, out, header=_header(generation.census))
    census_path.parent.mkdir(parents=True, exist_ok=True)
    census_path.write_text(render_census(generation.census), encoding="utf-8")


def render_summary(census: dict[str, Any], out: Path, census_path: Path) -> str:
    every, walk, split = census["retention"]["all"], census["walk"], census["retained_by_split"]
    rate = census["retention_rate"]
    rate_text = "n/a" if rate is None else f"{rate:.1%}"
    shares = ", ".join(f"{cls} {share:.1%}" for cls, share in census["dev_share_by_class"].items())
    lines = [
        f"golden set at corpus {census['corpus_commit'][:10]}: {every['candidates']:,} "
        f"candidates, {every['retained']:,} retained ({rate_text})",
        f"  walk: {walk['revisions']:,} revisions, {walk['diffed']:,} diffed, "
        f"{walk['skipped_unknown_side']:,} with an unknown side, "
        f"{walk['unparseable_blobs']:,} unparseable blobs",
    ]
    for template, counts in census["rules"].items():
        steps = " -> ".join(f"{name} {n:,}" for name, n in counts.items())
        lines.append(f"  {template + ':':<9}{steps}")
    lines += [
        f"  dropped: {every[RANK_1]:,} answered by grep at rank 1, {every[RARE_TOKEN]:,} "
        f"share a rare token with gold, {every[UNMEASURED]:,} unmeasured",
        f"  retained: {split['dev']:,} dev, {split['test']:,} test; dev share {shares or '-'}",
        f"  wrote {out} and {census_path}",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """`pace golden generate`: write the golden set and its census."""
    parser = argparse.ArgumentParser(
        prog="pace golden generate",
        description="Generate every question ADR-0003 §3 allows, with its leakage facts.",
    )
    parser.add_argument("--corpus", default=None, help="corpus path (default: settings)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"default: {DEFAULT_OUT}")
    parser.add_argument("--census", default=str(DEFAULT_CENSUS), help=f"default: {DEFAULT_CENSUS}")
    args = parser.parse_args(list(argv) if argv is not None else None)

    from provenance.graph.db import connection
    from provenance.graph.repos import UnknownRepository, resolve_repo_id

    corpus = Path(args.corpus) if args.corpus else Path(settings.corpus_path)
    try:
        with connection() as conn:
            repo_id = resolve_repo_id(conn, settings.repo_owner, settings.repo_name)
            generation = generate(conn, repo_id, corpus)
    except (UnknownRepository, NothingToGenerate) as exc:
        print(f"nothing to generate: {exc}", file=sys.stderr)
        return 2
    except GenerationInvariantError as exc:
        print(f"GENERATION CHECK FAILED: {exc}. Nothing was written.", file=sys.stderr)
        return 1

    out, census_path = Path(args.out), Path(args.census)
    write_outputs(generation, out, census_path)
    print(render_summary(generation.census, out, census_path))
    return 0
