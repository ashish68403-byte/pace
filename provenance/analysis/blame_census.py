"""`pace measure blame`: what slice C2 stored, counted (tasks/symbols-blame/001).

Reads `blame_ranges` and `line_log_commits` back for the retained dev questions' functions
and writes `results/blame_arms.json`:

* the inputs, and the commands that made the rows;
* trap 3 on these functions' lines: how many plain blame puts on a mechanical commit, and how
  many still sit on one with the mechanical list ignored (stored as NULL);
* how many commits arm A, unfiltered blame and arm L hold per function;
* whether the 2025 directory move stops either tool, which ADR-0005 left to this slice.

It scores nothing against gold: that is slice C4's, after the pre-registration.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from provenance.config import settings
from provenance.eval.metrics import percentile
from provenance.graph.tables import BLAME_RANGES, COMMITS, LINE_LOG_COMMITS
from provenance.ingest.blame import Target, file_ids, golden_target, renames, show
from provenance.parse.symbols import symbol_spans, symbols

DEFAULT_OUT: Final = Path("results") / "blame_arms.json"
#: "Move airflow sources to airflow-core package (#47798)", 2025-03-21.
MOVE_COMMIT: Final = "243fe86d4b3e59bb12977b3e36ca3f2ed27ca0f8"

_ALGOS: Final = {
    BLAME_RANGES: f"SELECT DISTINCT blame_algo FROM {BLAME_RANGES} "
    "WHERE repo_id = %s AND snapshot_sha = %s",
    LINE_LOG_COMMITS: f"SELECT DISTINCT algo FROM {LINE_LOG_COMMITS} "
    "WHERE repo_id = %s AND snapshot_sha = %s",
}
_COUNT: Final = "SELECT count(*) FROM {} WHERE repo_id = %s AND snapshot_sha = %s"
_RANGES: Final = (
    "SELECT lower(line_range), upper(line_range), orig_commit_sha, orig_path, "
    f"effective_commit_sha FROM {BLAME_RANGES} WHERE file_id = %s AND snapshot_sha = %s"
)
_LOGS: Final = (
    f"SELECT qualified_name, commit_sha FROM {LINE_LOG_COMMITS} "
    "WHERE repo_id = %s AND file_id = %s AND snapshot_sha = %s"
)
_COMMITS: Final = f"SELECT commit_sha, topo_order, is_mechanical FROM {COMMITS} WHERE repo_id = %s"
_MOVE_KEYS: Final = (
    "question_files_the_move_renamed",
    "functions_in_those_files",
    "existed_before_the_move",
    "line_log_reaches_before_the_move",
    "blame_owner_at_the_old_path",
)


class CensusRefused(RuntimeError):
    """The census needs rows the build has not stored; nothing was written."""


def _summary(counts: Sequence[int]) -> dict[str, float | int]:
    return {
        "median": round(percentile(counts, 50), 2),
        "p90": round(percentile(counts, 90), 2),
        "max": max(counts),
        "zero": sum(1 for count in counts if count == 0),
    }


def _made_by(conn: Any, table: str, repo_id: int, pinned: str) -> str:
    found = [str(algo) for (algo,) in conn.execute(_ALGOS[table], (repo_id, pinned))]
    if len(found) != 1:
        raise CensusRefused(
            f"{table} holds rows from {len(found)} commands, not one: run `pace blame build`"
        )
    return found[0]


def _count(conn: Any, table: str, repo_id: int, pinned: str) -> int:
    (count,) = conn.execute(_COUNT.format(table), (repo_id, pinned)).fetchone()
    return int(count)


def census(
    conn: Any, *, repo: Path, repo_id: int, target: Target, move_commit: str = MOVE_COMMIT
) -> dict[str, Any]:
    """Count what the build stored for `target`; refuses while any function lacks rows."""
    pinned = target.pinned
    made_by = _made_by(conn, BLAME_RANGES, repo_id, pinned)
    logged_by = _made_by(conn, LINE_LOG_COMMITS, repo_id, pinned)
    order: dict[str, int] = {}
    mechanical: set[str] = set()
    for sha, topo, is_mechanical in conn.execute(_COMMITS, (repo_id,)):
        order[str(sha)] = int(topo)
        if is_mechanical:
            mechanical.add(str(sha))
    renamed = renames(repo, move_commit) if move_commit in order else {}
    move_order = order.get(move_commit, 0)
    by_path: dict[str, list[str]] = defaultdict(list)
    for path, name in target.functions:
        by_path[path].append(name)
    ids = file_ids(conn, repo_id, sorted(by_path))

    arm_a: list[int] = []
    unfiltered: list[int] = []
    arm_l: list[int] = []
    lines = {"total": 0, "plain_owner_mechanical": 0, "filtered_owner_null": 0}
    move = dict.fromkeys(_MOVE_KEYS, 0)
    for path in sorted(by_path):
        spans = symbol_spans(show(repo, pinned, path), path) or {}
        owner: dict[int, tuple[str, str, str | None]] = {}
        for low, high, orig, orig_path, effective in conn.execute(_RANGES, (ids[path], pinned)):
            kept = None if effective is None else str(effective)
            for line in range(int(low), int(high)):
                owner[line] = (str(orig), str(orig_path), kept)
        logged: dict[str, set[str]] = defaultdict(set)
        for name, sha in conn.execute(_LOGS, (repo_id, ids[path], pinned)):
            logged[str(name)].add(str(sha))
        old_path = renamed.get(path)
        before: dict[str, str] = {}
        if old_path is not None:
            move["question_files_the_move_renamed"] += 1
            before = symbols(show(repo, f"{move_commit}^", old_path), old_path) or {}
        seen: set[int] = set()
        for name in sorted(by_path[path]):
            if name not in spans:
                raise CensusRefused(f"{path}: {name!r} is not a function at {pinned[:10]}")
            mine = {line for first, last in spans[name] for line in range(first, last + 1)}
            if not mine.issubset(owner) or not logged.get(name):
                raise CensusRefused(f"{path}::{name} has no stored rows: run `pace blame build`")
            owners = [owner[line] for line in sorted(mine)]
            arm_a.append(len({effective for _, _, effective in owners if effective is not None}))
            unfiltered.append(len({orig for orig, _, _ in owners}))
            arm_l.append(len(logged[name] - mechanical))
            seen |= mine
            if old_path is not None:
                move["functions_in_those_files"] += 1
                if name in before:
                    move["existed_before_the_move"] += 1
                    move["line_log_reaches_before_the_move"] += any(
                        order[sha] < move_order for sha in logged[name]
                    )
                    move["blame_owner_at_the_old_path"] += any(
                        where == old_path for _, where, _ in owners
                    )
        for line in seen:
            orig, _, effective = owner[line]
            lines["total"] += 1
            lines["plain_owner_mechanical"] += orig in mechanical
            lines["filtered_owner_null"] += effective is None

    total = max(lines["total"], 1)
    return {
        "schema": 1,
        "inputs": {
            "golden_sha256": target.source_sha256,
            "pinned": pinned,
            "blame": made_by,
            "line_log": logged_by,
            "questions": target.questions,
            "functions": len(target.functions),
            "files": len(by_path),
        },
        "stored": {
            BLAME_RANGES: _count(conn, BLAME_RANGES, repo_id, pinned),
            LINE_LOG_COMMITS: _count(conn, LINE_LOG_COMMITS, repo_id, pinned),
        },
        "lines": {
            **lines,
            "plain_owner_mechanical_share": round(lines["plain_owner_mechanical"] / total, 4),
            "filtered_owner_null_share": round(lines["filtered_owner_null"] / total, 4),
        },
        "commits_per_function": {
            "arm_a": _summary(arm_a),
            "blame_unfiltered": _summary(unfiltered),
            "arm_l": _summary(arm_l),
        },
        "move": {"commit": move_commit, **move},
    }


def render_json(counted: dict[str, Any]) -> str:
    """Sorted keys and no timestamps: the same rows always render the same bytes."""
    return json.dumps(counted, indent=2, sort_keys=True) + "\n"


def render_summary(counted: dict[str, Any], out: Path) -> str:
    inputs, lines, move = counted["inputs"], counted["lines"], counted["move"]
    per = counted["commits_per_function"]

    def sizes(arm: str) -> str:
        return f"{per[arm]['median']} / {per[arm]['p90']} / {per[arm]['max']}"

    return "\n".join(
        [
            f"blame arms at {inputs['pinned'][:10]}: {inputs['functions']:,} functions in "
            f"{inputs['files']} files ({inputs['questions']:,} questions)",
            f"  trap 3: {lines['plain_owner_mechanical_share']:.1%} of {lines['total']:,} lines "
            "have a mechanical plain owner; "
            f"{lines['filtered_owner_null_share']:.1%} keep one with the list ignored",
            f"  commits per function, median / p90 / max: arm A {sizes('arm_a')}, "
            f"unfiltered {sizes('blame_unfiltered')}, arm L {sizes('arm_l')}",
            f"  the move: of {move['existed_before_the_move']} functions that existed before it, "
            f"git log -L reaches past it for {move['line_log_reaches_before_the_move']} and "
            f"blame for {move['blame_owner_at_the_old_path']}",
            f"  wrote {out}",
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    """`pace measure blame`: write results/blame_arms.json."""
    from provenance.eval.runner import FULL_GOLDEN
    from provenance.graph.db import connection
    from provenance.graph.repos import UnknownRepository, resolve_repo_id

    parser = argparse.ArgumentParser(
        prog="pace measure blame",
        description="What slice C2 stored, counted: trap 3, the arms' sizes and the 2025 move.",
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
