"""`pace measure lineage`: what slice C3a stored, counted and checked against `git log -L`.

docs/tasks/function-identity/002_lineage_engine.md. Reads `symbol_lineage`, `symbol_versions`,
the ambiguous `lineage_edges` and the lineage `dead_letters`, and writes `results/lineage.json`:
the counts; Airflow's identity-break rate among the functions alive at the pinned commit; arm C's
size for the questions' functions; and PREREGISTRATION.md's agreement check with `git log -L`, on
the sample the spec fixes. Nothing in it is scored against gold.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from provenance.analysis.blame_census import MOVE_COMMIT
from provenance.config import settings
from provenance.eval.metrics import bootstrap_ci, percentile, set_f1, wilson_interval
from provenance.eval.preregistration import AGREEMENT_MIN_F1
from provenance.graph.tables import (
    COMMITS,
    DEAD_LETTERS,
    FILES,
    LINE_LOG_COMMITS,
    LINEAGE_EDGES,
    SYMBOL_LINEAGE,
    SYMBOL_VERSIONS,
)
from provenance.ingest.blame import Target, golden_target, renames, show
from provenance.lineage.build import DETECTOR, STAGE
from provenance.parse.symbols import symbols

DEFAULT_OUT: Final = Path("results") / "lineage.json"
AGREEMENT_SALT: Final = "pace-agreement-v1:"
AGREEMENT_GROUP: Final = 50
_KINDS: Final = ("function_rename", "cross_file_move", "file_rename", "any")

_DETECTORS: Final = f"SELECT DISTINCT detector_version FROM {SYMBOL_LINEAGE} WHERE repo_id = %s"
_COMMITS: Final = f"SELECT commit_sha, topo_order, is_mechanical FROM {COMMITS} WHERE repo_id = %s"
_VERSIONS: Final = (
    "SELECT v.lineage_id, v.commit_sha, v.match_stage, v.is_rename, v.is_move "
    f"FROM {SYMBOL_VERSIONS} v JOIN {SYMBOL_LINEAGE} l USING (lineage_id) "
    "WHERE l.repo_id = %s ORDER BY v.lineage_id, v.commit_topo_order"
)
_LINEAGES: Final = (
    "SELECT lineage_id, death_commit, current_path, current_qualified_name "
    f"FROM {SYMBOL_LINEAGE} WHERE repo_id = %s"
)
_AMBIGUOUS: Final = (
    f"SELECT count(DISTINCT (at_commit, evidence->>'key')) FROM {LINEAGE_EDGES} "
    "WHERE repo_id = %s AND kind = 'ambiguous'"
)
_REFUSED: Final = f"SELECT count(*) FROM {DEAD_LETTERS} WHERE repo_id = %s AND stage = %s"
_LOGS: Final = (
    f"SELECT f.path, l.qualified_name, l.commit_sha FROM {LINE_LOG_COMMITS} l "
    f"JOIN {FILES} f USING (file_id) WHERE l.repo_id = %s AND l.snapshot_sha = %s"
)


class CensusRefused(RuntimeError):
    """The census needs rows the builds have not stored; nothing was written."""


def agreement_sample(
    groups: Mapping[str, Sequence[tuple[str, str]]], *, size: int = AGREEMENT_GROUP
) -> dict[str, list[tuple[str, str]]]:
    """The spec's sample, each group in salted-hash order.

    Every moved function `git log -L` stops for, then ones it reaches past, up to `size` moved
    in all; and the first `size` others.
    """

    def order(functions: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
        return sorted(
            functions,
            key=lambda f: hashlib.sha256(f"{AGREEMENT_SALT}{f[0]}::{f[1]}".encode()).hexdigest(),
        )

    stops = order(groups["moved_stops"])[:size]
    return {
        "moved_stops": stops,
        "moved_reaches": order(groups["moved_reaches"])[: max(size - len(stops), 0)],
        "other": order(groups["other"])[:size],
    }


def _summary(counts: Sequence[int]) -> dict[str, float | int]:
    return {
        "median": round(percentile(counts, 50), 2),
        "p90": round(percentile(counts, 90), 2),
        "max": max(counts),
        "zero": sum(1 for count in counts if count == 0),
    }


def _rate(count: int, n: int) -> dict[str, Any]:
    lo, hi = wilson_interval(count, n)
    share = round(count / max(n, 1), 4)
    return {"count": count, "share": share, "wilson": [round(lo, 4), round(hi, 4)]}


def _ci(values: Sequence[float]) -> dict[str, float | int]:
    found = bootstrap_ci(values)
    return {
        "point": round(found.point, 4),
        "lo": round(found.lo, 4),
        "hi": round(found.hi, 4),
        "n": found.n,
    }


def census(
    conn: Any,
    *,
    repo: Path,
    repo_id: int,
    target: Target,
    move_commit: str = MOVE_COMMIT,
    group_size: int = AGREEMENT_GROUP,
) -> dict[str, Any]:
    """Count what the lineage build stored; refuses before a build or for a missing identity."""
    detectors = sorted(str(d) for (d,) in conn.execute(_DETECTORS, (repo_id,)))
    if detectors != [DETECTOR]:
        found = detectors or "no rows"
        raise CensusRefused(f"{SYMBOL_LINEAGE} holds {found}: run `pace lineage build`")
    order: dict[str, int] = {}
    mechanical: set[str] = set()
    for sha, topo, is_mechanical in conn.execute(_COMMITS, (repo_id,)):
        order[str(sha)] = int(topo)
        if is_mechanical:
            mechanical.add(str(sha))
    versions: dict[int, list[tuple[str, str, bool, bool]]] = defaultdict(list)
    stages: Counter[str] = Counter()
    for lineage_id, commit, stage, is_rename, is_move in conn.execute(_VERSIONS, (repo_id,)):
        versions[int(lineage_id)].append((str(commit), str(stage), bool(is_rename), bool(is_move)))
        stages[str(stage)] += 1
    alive: dict[tuple[str, str], int] = {}
    deaths = total = 0
    for lineage_id, death, path, name in conn.execute(_LINEAGES, (repo_id,)):
        total += 1
        if death is None:
            alive[(str(path), str(name))] = int(lineage_id)
        else:
            deaths += 1
    (ambiguous,) = conn.execute(_AMBIGUOUS, (repo_id,)).fetchone()
    (refused,) = conn.execute(_REFUSED, (repo_id, STAGE)).fetchone()

    def breaks(skip: str | None) -> dict[str, Any]:
        counts = dict.fromkeys(_KINDS, 0)
        for lineage_id in alive.values():
            rows = [row for row in versions[lineage_id] if row[0] != skip]
            found = {
                "function_rename": any(is_rename for _, _, is_rename, _ in rows),
                "cross_file_move": any(stage == "move" for _, stage, _, _ in rows),
                "file_rename": any(is_move and stage != "move" for _, stage, _, is_move in rows),
            }
            found["any"] = any(found.values())
            for kind, hit in found.items():
                counts[kind] += int(hit)
        return {"n": len(alive), **{kind: _rate(counts[kind], len(alive)) for kind in _KINDS}}

    def lineage_commits(function: tuple[str, str]) -> set[str]:
        if function not in alive:
            raise CensusRefused(
                f"{function[0]}::{function[1]} has no identity alive at {target.pinned[:10]}"
            )
        return {commit for commit, _, _, _ in versions[alive[function]]} - mechanical

    arm_c = [len(lineage_commits(function)) for function in target.functions]

    logs: dict[tuple[str, str], set[str]] = defaultdict(set)
    for path, name, commit in conn.execute(_LOGS, (repo_id, target.pinned)):
        logs[(str(path), str(name))].add(str(commit))
    renamed = renames(repo, move_commit) if move_commit in order else {}
    before: dict[str, dict[str, str]] = {}
    groups: dict[str, list[tuple[str, str]]] = {"moved_stops": [], "moved_reaches": [], "other": []}
    for path, name in target.functions:
        old = renamed.get(path)
        if old is not None and old not in before:
            before[old] = symbols(show(repo, f"{move_commit}^", old), old) or {}
        if old is not None and name in before[old]:
            reaches = any(order[c] < order[move_commit] for c in logs.get((path, name), ()))
            groups["moved_reaches" if reaches else "moved_stops"].append((path, name))
        else:
            groups["other"].append((path, name))
    sample = agreement_sample(groups, size=group_size)
    scores: dict[str, list[float]] = {}
    for group, functions in sample.items():
        scores[group] = []
        for function in functions:
            if not logs.get(function):
                raise CensusRefused(
                    f"{function[0]}::{function[1]} has no stored git log -L: run `pace blame build`"
                )
            found_commits = lineage_commits(function)
            scores[group].append(set_f1(found_commits, logs[function] - mechanical))
    overall = _ci([score for group in sample for score in scores[group]])
    renames_count = sum(1 for rows in versions.values() for row in rows if row[2])
    file_renames = sum(
        1 for rows in versions.values() for row in rows if row[3] and row[1] != "move"
    )
    return {
        "schema": 1,
        "inputs": {
            "golden_sha256": target.source_sha256,
            "pinned": target.pinned,
            "detector": DETECTOR,
            "functions": len(target.functions),
        },
        "identities": {"total": total, "alive_at_pinned": len(alive), "deaths": deaths},
        "versions": {
            "total": sum(stages.values()),
            "by_stage": dict(sorted(stages.items())),
            "renames": renames_count,
            "cross_file_moves": stages.get("move", 0),
            "file_renames": file_renames,
        },
        "ambiguous_cases": int(ambiguous),
        "refused_revisions": int(refused),
        "identity_breaks": {
            "move_commit": move_commit,
            "with_the_move": breaks(None),
            "without_it": breaks(move_commit),
        },
        "arm_c_per_function": _summary(arm_c),
        "agreement": {
            "threshold": AGREEMENT_MIN_F1,
            "sample": {group: len(functions) for group, functions in sample.items()},
            "mean_f1": overall,
            "groups": {group: _ci(values) for group, values in scores.items() if values},
            "verdict": "meets" if overall["point"] >= AGREEMENT_MIN_F1 else "misses",
        },
    }


def render_json(counted: dict[str, Any]) -> str:
    """Sorted keys and no timestamps: the same rows always render the same bytes."""
    return json.dumps(counted, indent=2, sort_keys=True) + "\n"


def render_summary(counted: dict[str, Any], out: Path) -> str:
    ids, agreement = counted["identities"], counted["agreement"]
    with_move = counted["identity_breaks"]["with_the_move"]
    without = counted["identity_breaks"]["without_it"]
    mean = agreement["mean_f1"]
    return "\n".join(
        [
            f"lineage: {ids['total']:,} identities ({ids['alive_at_pinned']:,} alive), "
            f"{counted['versions']['total']:,} versions, {counted['ambiguous_cases']} ambiguous, "
            f"{counted['refused_revisions']} refused revisions",
            f"  identity breaks among the alive: {with_move['any']['share']:.1%} with the 2025 "
            f"move, {without['any']['share']:.1%} without it",
            f"  agreement with git log -L: mean F1 {mean['point']} [{mean['lo']}, {mean['hi']}], "
            f"{agreement['verdict']} {agreement['threshold']}",
            f"  wrote {out}",
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    """`pace measure lineage`: write results/lineage.json."""
    from provenance.eval.runner import FULL_GOLDEN
    from provenance.graph.db import connection
    from provenance.graph.repos import UnknownRepository, resolve_repo_id

    parser = argparse.ArgumentParser(
        prog="pace measure lineage",
        description="What slice C3a stored, counted, and its agreement with git log -L.",
    )
    parser.add_argument("--golden", default=str(FULL_GOLDEN), help=f"default: {FULL_GOLDEN}")
    parser.add_argument("--corpus", default=None, help="corpus path (default: settings)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"default: {DEFAULT_OUT}")
    args = parser.parse_args(list(argv) if argv is not None else None)
    corpus = Path(args.corpus) if args.corpus else Path(settings.corpus_path)
    try:
        target = golden_target(Path(args.golden))
        with connection() as conn:
            repo_id = resolve_repo_id(conn, settings.repo_owner, settings.repo_name)
            counted = census(conn, repo=corpus, repo_id=repo_id, target=target)
    except (UnknownRepository, RuntimeError) as exc:  # CensusRefused, BlameRefused, GitFailed
        print(f"nothing to measure: {exc}", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_json(counted), encoding="utf-8")
    print(render_summary(counted, out))
    return 0
