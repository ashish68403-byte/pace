"""How much of a file's history does a current-path view hide?

Checkpoint A's measurement, specified in docs/tasks/ingest/003_history_truncation.md.
For each in-scope file, three first-parent sets of commits:

    P  current path   git log --first-parent --format=%H -- <today's path>
    R  rename-aware   the file's revisions in the graph `pace ingest` built
    F  git follow     git log --first-parent --follow --format=%H -- <today's path>

From them: the share of R that P never shows, how much of that hidden history
references a PR, and how well R agrees with F -- "agreement", not accuracy, because
F is a similarity heuristic too, so where they disagree this reports examples rather
than declaring a winner.

Two rules make the numbers safe to quote:

* A built-in check. P from git must equal the graph's own current-path set (the
  revisions whose diff touched today's path) for every file. Two independent
  computations have to agree, or the command fails and writes nothing.
* It refuses to measure nothing. If the graph has no history for the scope, it exits
  2 instead of printing zeros.

No confidence intervals, on purpose: the 303 files are the whole population, not a
sample, so there is no sampling error to bound. The uncertainty is in the rename
heuristics, and the agreement with F is what quantifies it.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from provenance.config import settings
from provenance.graph.tables import COMMITS, FILE_REVISIONS, FILES
from provenance.ingest.scope import SCOPE_VERSION, load_scope

SCHEMA: Final = "pace.history_truncation/v1"
DEFAULT_OUT: Final = Path("results") / "history_truncation.json"
PR_REFERENCE_RE: Final = re.compile(r"\(#\d+\)")
EXAMPLES_PER_FILE: Final = 5
PROGRESS_EVERY: Final = 50

COMMANDS: Final = {
    "current_path": "git log --first-parent --format=%H -- <path>",
    "follow": "git log --first-parent --follow --format=%H -- <path>",
    "rename_aware": "file_revisions JOIN files ON file_id WHERE files.path = <path>",
}

_HISTORY_SQL: Final = f"""
    SELECT r.commit_sha, r.commit_topo_order, r.path_at_commit, r.old_path,
           r.change_type::text, c.subject,
           to_char(c.authored_at AT TIME ZONE 'UTC', 'YYYY-MM-DD')
      FROM {FILE_REVISIONS} AS r
      JOIN {FILES} AS f ON f.file_id = r.file_id
      JOIN {COMMITS} AS c ON c.repo_id = r.repo_id AND c.commit_sha = r.commit_sha
     WHERE f.repo_id = %s AND f.path = %s
     ORDER BY r.commit_topo_order DESC
"""


class NothingToMeasure(RuntimeError):
    """The graph holds no history for the files asked about. Run `pace ingest` first."""


@dataclass(frozen=True, slots=True)
class Revision:
    """One revision of one file, as the graph stores it."""

    commit_sha: str
    topo_order: int
    path_at_commit: str
    old_path: str | None
    change_type: str
    subject: str
    authored_on: str


@dataclass(frozen=True, slots=True)
class FileMeasurement:
    path: str
    n_current_path: int
    n_rename_aware: int
    n_follow: int
    n_hidden: int
    n_hidden_with_pr_reference: int
    n_agree_with_follow: int
    hidden_share: float | None
    follow_precision: float | None
    follow_recall: float | None
    follow_f1: float | None
    current_path_check: bool
    current_path_mismatch: tuple[str, ...]
    only_in_graph: tuple[str, ...]
    only_in_follow: tuple[str, ...]
    rename_aware: frozenset[str]
    hidden: frozenset[str]

    def as_row(self) -> dict[str, Any]:
        """The JSON row: everything except the two working sets."""
        return {
            "path": self.path,
            "n_current_path": self.n_current_path,
            "n_rename_aware": self.n_rename_aware,
            "n_follow": self.n_follow,
            "n_hidden": self.n_hidden,
            "n_hidden_with_pr_reference": self.n_hidden_with_pr_reference,
            "n_agree_with_follow": self.n_agree_with_follow,
            "hidden_share": self.hidden_share,
            "follow_precision": self.follow_precision,
            "follow_recall": self.follow_recall,
            "follow_f1": self.follow_f1,
            "current_path_check": self.current_path_check,
            "current_path_mismatch": list(self.current_path_mismatch),
            "only_in_graph": list(self.only_in_graph),
            "only_in_follow": list(self.only_in_follow),
        }


def graph_history(conn: Any, repo_id: int, path: str) -> list[Revision]:
    """The file's revisions in the graph, newest first."""
    return [Revision(*row) for row in conn.execute(_HISTORY_SQL, (repo_id, path)).fetchall()]


def git_commits(root: Path, path: str, *, follow: bool) -> list[str]:
    """What git itself lists for ``path`` on the first-parent walk, newest first."""
    command = ["git", "-C", str(root), "log", "--first-parent", "--format=%H"]
    if follow:
        command.append("--follow")
    command += ["--", path]
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout.split()


def touches_todays_path(revision: Revision, path: str) -> bool:
    """Would ``git log -- <path>`` list this revision's commit?

    Yes when the diff touched ``path`` itself: the file was at ``path`` after the
    change, or was renamed away from it.
    """
    return revision.path_at_commit == path or revision.old_path == path


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 4)


def measure_file(
    path: str,
    *,
    current: Sequence[str],
    revisions: Sequence[Revision],
    follow: Sequence[str],
) -> FileMeasurement:
    """Compare P (``current``), R (``revisions``) and F (``follow``) for one file."""
    p, f = set(current), set(follow)
    r = {rev.commit_sha for rev in revisions}
    subjects = {rev.commit_sha: rev.subject for rev in revisions}
    topo = {rev.commit_sha: rev.topo_order for rev in revisions}
    hidden = r - p
    agree = r & f
    graph_current = {rev.commit_sha for rev in revisions if touches_todays_path(rev, path)}
    return FileMeasurement(
        path=path,
        n_current_path=len(p),
        n_rename_aware=len(r),
        n_follow=len(f),
        n_hidden=len(hidden),
        n_hidden_with_pr_reference=sum(
            1 for sha in hidden if PR_REFERENCE_RE.search(subjects[sha])
        ),
        n_agree_with_follow=len(agree),
        hidden_share=_ratio(len(hidden), len(r)),
        follow_precision=_ratio(len(agree), len(r)),
        follow_recall=_ratio(len(agree), len(f)),
        follow_f1=_ratio(2 * len(agree), len(r) + len(f)),
        current_path_check=p == graph_current,
        current_path_mismatch=tuple(sorted(p ^ graph_current)),
        only_in_graph=tuple(sorted(r - f, key=lambda sha: -topo[sha])[:EXAMPLES_PER_FILE]),
        only_in_follow=tuple(sorted(f - r)[:EXAMPLES_PER_FILE]),
        rename_aware=frozenset(r),
        hidden=frozenset(hidden),
    )


def aggregate(measurements: Sequence[FileMeasurement], subjects: dict[str, str]) -> dict[str, Any]:
    """Census-level numbers over every measured file."""
    if not measurements:
        raise ValueError("aggregate() needs at least one measured file")
    shares = [m.hidden_share for m in measurements if m.hidden_share is not None]
    quartiles = (
        statistics.quantiles(shares, n=4, method="inclusive") if len(shares) > 1 else shares * 3
    )
    hidden_everywhere: dict[str, bool] = {}
    for m in measurements:
        for sha in m.rename_aware:
            hidden_everywhere[sha] = hidden_everywhere.get(sha, True) and sha in m.hidden
    fully_hidden = sorted(sha for sha, hidden in hidden_everywhere.items() if hidden)
    total_hidden = sum(m.n_hidden for m in measurements)
    total_rename_aware = sum(m.n_rename_aware for m in measurements)
    total_agree = sum(m.n_agree_with_follow for m in measurements)
    over_half = sum(1 for share in shares if share > 0.5)
    return {
        "files_measured": len(measurements),
        "hidden_share_q1": round(quartiles[0], 4),
        "hidden_share_median": round(quartiles[1], 4),
        "hidden_share_q3": round(quartiles[2], 4),
        "files_hiding_more_than_half": over_half,
        "files_hiding_more_than_half_share": _ratio(over_half, len(shares)),
        "pooled_hidden_share": _ratio(total_hidden, total_rename_aware),
        "pooled_hidden_with_pr_reference_share": _ratio(
            sum(m.n_hidden_with_pr_reference for m in measurements), total_hidden
        ),
        "commits_touching_scope": len(hidden_everywhere),
        "commits_hidden_everywhere": len(fully_hidden),
        "commits_hidden_everywhere_share": _ratio(len(fully_hidden), len(hidden_everywhere)),
        "commits_hidden_everywhere_with_pr_reference": sum(
            1 for sha in fully_hidden if PR_REFERENCE_RE.search(subjects.get(sha, ""))
        ),
        "follow_pooled_precision": _ratio(total_agree, total_rename_aware),
        "follow_pooled_recall": _ratio(total_agree, sum(m.n_follow for m in measurements)),
        "files_agreeing_exactly_with_follow": sum(
            1 for m in measurements if m.n_agree_with_follow == m.n_rename_aware == m.n_follow
        ),
    }


def _git_output(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def build_report(
    conn: Any, repo_id: int, root: Path, scope: Sequence[str], *, progress: bool = False
) -> dict[str, Any]:
    """Measure every scope path; refuse if the graph has no history for any of them."""
    paths = sorted(set(scope))
    measurements: list[FileMeasurement] = []
    subjects: dict[str, str] = {}
    missing: list[str] = []
    for index, path in enumerate(paths, start=1):
        revisions = graph_history(conn, repo_id, path)
        if not revisions:
            missing.append(path)
            continue
        subjects.update((rev.commit_sha, rev.subject) for rev in revisions)
        measurements.append(
            measure_file(
                path,
                current=git_commits(root, path, follow=False),
                revisions=revisions,
                follow=git_commits(root, path, follow=True),
            )
        )
        if progress and index % PROGRESS_EVERY == 0:
            print(f"measured {index:,} of {len(paths):,} files", file=sys.stderr)
    if not measurements:
        raise NothingToMeasure(
            f"the graph has no history for any of the {len(paths):,} in-scope files; "
            "run `pace ingest` first"
        )
    if missing:
        raise NothingToMeasure(
            f"{len(missing):,} in-scope files have no history in the graph "
            f"(first: {missing[0]}); run a full `pace ingest`"
        )
    return {
        "schema": SCHEMA,
        "corpus_commit": _git_output(root, "rev-parse", "HEAD"),
        "scope_version": SCOPE_VERSION,
        "git_version": _git_output(root, "--version"),
        "commands": dict(COMMANDS),
        "current_path_check": {
            "files_checked": len(measurements),
            "files_failed": [m.path for m in measurements if not m.current_path_check],
        },
        "aggregates": aggregate(measurements, subjects),
        "files": [m.as_row() for m in measurements],
    }


def render_json(report: dict[str, Any]) -> str:
    """Sorted keys and no timestamps, so the same graph always renders the same bytes."""
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def render_summary(report: dict[str, Any], out: Path) -> str:
    a = report["aggregates"]
    check = report["current_path_check"]
    agreeing = check["files_checked"] - len(check["files_failed"])
    return "\n".join(
        [
            f"history truncation over {a['files_measured']:,} in-scope files "
            f"at corpus {report['corpus_commit'][:10]}",
            f"  a current-path view hides {_pct(a['pooled_hidden_share'])} of "
            "(file, commit) history",
            f"  per file: median {_pct(a['hidden_share_median'])} hidden "
            f"(IQR {_pct(a['hidden_share_q1'])} to {_pct(a['hidden_share_q3'])}); "
            f"{a['files_hiding_more_than_half']:,} files hide more than half",
            f"  {a['commits_hidden_everywhere']:,} of {a['commits_touching_scope']:,} commits "
            "are hidden from every current-path lookup that could show them",
            f"  {_pct(a['pooled_hidden_with_pr_reference_share'])} of the hidden history "
            "references a PR",
            f"  agreement with git --follow: precision {a['follow_pooled_precision']}, "
            f"recall {a['follow_pooled_recall']}; "
            f"{a['files_agreeing_exactly_with_follow']:,} files agree exactly",
            f"  current-path check: {agreeing:,} of {check['files_checked']:,} files "
            "agree with git",
            f"  wrote {out}",
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    """`pace measure truncation`: write results/history_truncation.json."""
    parser = argparse.ArgumentParser(
        prog="pace measure truncation",
        description="How much of each in-scope file's history a current-path view hides.",
    )
    parser.add_argument("--repo", default=None, help="corpus path (default: settings.corpus_path)")
    parser.add_argument("--scope", dest="scope_path", default=None, help="frozen scope file")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"output (default: {DEFAULT_OUT})")
    args = parser.parse_args(list(argv) if argv is not None else None)

    from provenance.graph.db import connection
    from provenance.graph.repos import UnknownRepository, resolve_repo_id

    root = Path(args.repo) if args.repo else Path(settings.corpus_path)
    try:
        scope = load_scope(Path(args.scope_path) if args.scope_path else None)
        with connection() as conn:
            repo_id = resolve_repo_id(conn, settings.repo_owner, settings.repo_name)
            report = build_report(conn, repo_id, root, scope, progress=True)
    except (FileNotFoundError, UnknownRepository, NothingToMeasure) as exc:
        print(f"nothing to measure: {exc}")
        return 2

    failed = report["current_path_check"]["files_failed"]
    if failed:
        rows = {row["path"]: row for row in report["files"]}
        print(
            f"CHECK FAILED: git and the graph disagree on the current-path history of "
            f"{len(failed):,} files. Nothing was written."
        )
        for path in failed[:10]:
            print(f"  {path}: {rows[path]['current_path_mismatch'][:5]}")
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_json(report), encoding="utf-8")
    print(render_summary(report, out))
    return 0
