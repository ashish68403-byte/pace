"""The benchmark pool, counted: `pace measure pool` writes results/pool_census.json.

What slice B2 wrote, and why each PR link is or is not there
(docs/tasks/eval-harness/002_commit_message_pool.md). Counts and identifiers only --
no message text -- so the file can be committed. Sorted keys and no timestamps, so the
same graph always renders the same bytes.

A built-in check, as in the truncation measurement: the links stored in chunk_evidence
must equal the links the rule decides when it is recomputed here. Two computations have
to agree, or the command fails and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from provenance.config import settings
from provenance.graph.tables import CHUNK_EVIDENCE, CHUNKS, COMMITS
from provenance.ingest.message_pool import (
    APACHE_NUMBERING_STARTS,
    DROPPED_BEFORE_NUMBERING,
    DROPPED_IMPLAUSIBLE,
    LINKED,
    NO_REFERENCE,
    OUTCOMES,
    PR_NUMBER_FLOOR,
    PR_PATTERNS,
    PoolCommit,
    link_decisions,
)

SCHEMA: Final = "pace.pool_census/v1"
DEFAULT_OUT: Final = Path("results") / "pool_census.json"
LONG_MESSAGE_WORDS: Final = 300

_DOCUMENTS_SQL: Final = f"""
    SELECT c.commit_sha, c.topo_order, c.subject, c.message, c.is_merge, c.is_mechanical,
           d.content
    FROM {CHUNKS} d
    JOIN {COMMITS} c ON c.repo_id = d.repo_id AND d.path = 'commit/' || c.commit_sha
    WHERE d.repo_id = %s AND d.kind = 'commit'
    ORDER BY c.topo_order
"""
_EVIDENCE_SQL: Final = f"""
    SELECT d.path, e.evidence_key, e.source
    FROM {CHUNK_EVIDENCE} e JOIN {CHUNKS} d USING (chunk_id)
    WHERE e.repo_id = %s AND d.kind = 'commit'
"""
_TOPO_SQL: Final = f"SELECT topo_order FROM {COMMITS} WHERE repo_id = %s AND commit_sha = %s"


class NothingToMeasure(RuntimeError):
    """No message documents are stored: run `pace pool build` first."""


class CensusCheckFailed(RuntimeError):
    """The stored PR links differ from the links the rule decides."""


def percentile(values: Sequence[int], q: float) -> int:
    """Nearest-rank percentile, as Postgres's percentile_disc computes it."""
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


def _spread(values: Sequence[int]) -> dict[str, int]:
    return {
        "p50": percentile(values, 0.5),
        "p90": percentile(values, 0.9),
        "p99": percentile(values, 0.99),
        "max": max(values),
    }


def _git_output(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def build_census(
    conn: Any, repo_id: int, root: Path, *, numbering_starts: str = APACHE_NUMBERING_STARTS
) -> dict[str, Any]:
    """Count the stored pool, after checking its links against the rule."""
    rows = conn.execute(_DOCUMENTS_SQL, (repo_id,)).fetchall()
    if not rows:
        raise NothingToMeasure(
            f"repo {repo_id} has no message documents; run `pace pool build` first"
        )
    boundary = conn.execute(_TOPO_SQL, (repo_id, numbering_starts)).fetchone()
    if boundary is None:
        raise NothingToMeasure(f"the numbering boundary {numbering_starts} is not in the graph")
    commits = [PoolCommit(row[0], int(row[1]), row[2], row[3]) for row in rows]
    decisions = link_decisions(commits, numbering_starts_at=int(boundary[0]))
    decided = {(f"commit/{d.sha}", str(d.pr)) for d in decisions if d.outcome == LINKED}

    evidence = conn.execute(_EVIDENCE_SQL, (repo_id,)).fetchall()
    stored = {(path, key) for path, key, source in evidence if source == "linked"}
    if stored != decided:
        raise CensusCheckFailed(
            f"stored PR links differ from the rule: {len(decided - stored):,} missing, "
            f"{len(stored - decided):,} unexpected; rebuild with `pace pool build`"
        )

    outcomes = Counter(d.outcome for d in decisions)
    per_pr = Counter(key for _, key in stored)
    contents = [row[6] for row in rows]
    words = [len(content.split()) for content in contents]
    lines = [content.count("\n") + 1 for content in contents]
    return {
        "schema": SCHEMA,
        "corpus_commit": _git_output(root, "rev-parse", "HEAD"),
        "rule": {
            "patterns": list(PR_PATTERNS),
            "numbering_starts": numbering_starts,
            "pr_number_floor": PR_NUMBER_FLOOR,
        },
        "pool": {
            "documents": len(rows),
            "merges": sum(1 for row in rows if row[4]),
            "mechanical": sum(1 for row in rows if row[5]),
        },
        "links": {
            "candidates": len(rows) - outcomes[NO_REFERENCE],
            **{outcome: outcomes[outcome] for outcome in OUTCOMES},
            "dropped": {
                outcome: [{"commit": d.sha, "pr": d.pr} for d in decisions if d.outcome == outcome]
                for outcome in (DROPPED_BEFORE_NUMBERING, DROPPED_IMPLAUSIBLE)
            },
        },
        "evidence": {
            "self": sum(1 for _, _, source in evidence if source == "self"),
            "linked": len(stored),
            "distinct_prs": len(per_pr),
            "prs_with_two_or_more_documents": sum(1 for n in per_pr.values() if n > 1),
        },
        "message_length": {
            "words": _spread(words),
            "lines": _spread(lines),
            f"over_{LONG_MESSAGE_WORDS}_words": sum(1 for w in words if w > LONG_MESSAGE_WORDS),
        },
    }


def render_json(census: dict[str, Any]) -> str:
    """Sorted keys and no timestamps, so the same graph always renders the same bytes."""
    return json.dumps(census, indent=2, sort_keys=True) + "\n"


def render_summary(census: dict[str, Any], out: Path) -> str:
    pool, links, evidence = census["pool"], census["links"], census["evidence"]
    words = census["message_length"]["words"]
    return "\n".join(
        [
            f"commit-message pool at corpus {census['corpus_commit'][:10]}: "
            f"{pool['documents']:,} documents ({pool['merges']:,} merges, "
            f"{pool['mechanical']:,} mechanical)",
            f"  PR links: {links[LINKED]:,} linked to {evidence['distinct_prs']:,} PRs; "
            f"{links[NO_REFERENCE]:,} without a reference",
            f"  dropped: {links[DROPPED_BEFORE_NUMBERING]:,} before apache/airflow numbering, "
            f"{links[DROPPED_IMPLAUSIBLE]:,} implausible",
            f"  PRs with two or more documents: {evidence['prs_with_two_or_more_documents']:,}",
            f"  message words: median {words['p50']}, p90 {words['p90']}, p99 {words['p99']}, "
            f"max {words['max']:,}",
            f"  wrote {out}",
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    """`pace measure pool`: write results/pool_census.json."""
    parser = argparse.ArgumentParser(
        prog="pace measure pool",
        description="The benchmark pool, counted: documents, PR links, message lengths.",
    )
    parser.add_argument("--repo", default=None, help="corpus path (default: settings.corpus_path)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"output (default: {DEFAULT_OUT})")
    args = parser.parse_args(list(argv) if argv is not None else None)

    from provenance.graph.db import connection
    from provenance.graph.repos import UnknownRepository, resolve_repo_id

    root = Path(args.repo) if args.repo else Path(settings.corpus_path)
    try:
        with connection() as conn:
            repo_id = resolve_repo_id(conn, settings.repo_owner, settings.repo_name)
            census = build_census(conn, repo_id, root)
    except (UnknownRepository, NothingToMeasure) as exc:
        print(f"nothing to measure: {exc}")
        return 2
    except CensusCheckFailed as exc:
        print(f"CHECK FAILED: {exc}. Nothing was written.")
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_json(census), encoding="utf-8")
    print(render_summary(census, out))
    return 0
