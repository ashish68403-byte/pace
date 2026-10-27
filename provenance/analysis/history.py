"""`pace history <path>`: one file's history, marking what a current-path view misses.

Checkpoint A's demo (docs/tasks/ingest/003_history_truncation.md). It reads only the
graph, so it is instant and needs no corpus clone. Its current-path view is the
graph's own -- the revisions whose diff touched today's path -- which
`pace measure truncation` checks against git for every in-scope file.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from provenance.analysis.truncation import Revision, graph_history, touches_todays_path
from provenance.config import settings

LEGEND = "  + = hidden from a current-path view (git log -- <path> never lists it)"


def render_history(revisions: Sequence[Revision], path: str, *, limit: int | None = None) -> str:
    """Newest first: marker, date, short sha, path at that commit, subject; then the counts."""
    shown = list(revisions) if limit is None else list(revisions)[:limit]
    width = max((len(rev.path_at_commit) for rev in shown), default=0)
    lines = [f"history of {path}", LEGEND, ""]
    for rev in shown:
        mark = " " if touches_todays_path(rev, path) else "+"
        lines.append(
            f"{mark} {rev.authored_on}  {rev.commit_sha[:10]}  "
            f"{rev.path_at_commit:<{width}}  {rev.subject[:72]}"
        )
    if len(shown) < len(revisions):
        lines.append(f"  ... {len(revisions) - len(shown):,} older commits not shown")
    visible = sum(1 for rev in revisions if touches_todays_path(rev, path))
    hidden = len(revisions) - visible
    share = hidden / len(revisions) if revisions else 0.0
    lines += [
        "",
        f"current path: {visible:,} commits | rename-aware: {len(revisions):,} | "
        f"hidden: {hidden:,} ({share:.1%})",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pace history",
        description="One file's history, marking what a current-path view misses.",
    )
    parser.add_argument("path", help="today's path, e.g. airflow-core/src/airflow/models/dag.py")
    parser.add_argument("--limit", type=int, default=None, help="show only the newest N commits")
    args = parser.parse_args(list(argv) if argv is not None else None)

    from provenance.graph.db import connection
    from provenance.graph.repos import UnknownRepository, resolve_repo_id

    try:
        with connection() as conn:
            repo_id = resolve_repo_id(conn, settings.repo_owner, settings.repo_name)
            revisions = graph_history(conn, repo_id, args.path)
    except UnknownRepository as exc:
        print(exc)
        return 2
    if not revisions:
        print(
            f"no history for {args.path} in the graph: "
            "is it an in-scope path, and has `pace ingest` run?"
        )
        return 2
    print(render_history(revisions, args.path, limit=args.limit))
    return 0
