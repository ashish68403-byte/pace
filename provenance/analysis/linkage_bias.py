"""ADR-0004 §7: do the commits slice B2 could not link to a PR differ from those it could?

The review warned of a benchmark built only from commits with a recoverable PR link (Bird
et al., FSE 2009). ADR-0003 generates questions from every qualifying commit, linked or
not, so that bias does not arise as such. What remains is whether the unlinked commits --
whose gold is a commit message alone -- differ from the linked ones, by module, author and
year. Descriptive, not a test. Counts only: no author name leaves this module.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from provenance.graph.tables import CHUNKS, COMMITS, FILE_REVISIONS, FILES
from provenance.ingest.message_pool import (
    APACHE_NUMBERING_STARTS,
    LINKED,
    NO_REFERENCE,
    PoolCommit,
    link_decisions,
)

#: A module is the first directory under this root, by a file's path today. The path at
#: the commit changes with the 2025 move into airflow-core/, which would tie module to date.
MODULE_ROOT: Final = "airflow-core/src/airflow/"
TOP_LEVEL: Final = "(top level)"
OTHER: Final = "(other)"
UNKNOWN_AUTHOR: Final = "(unknown)"
TOP_AUTHORS: Final = 10
GROUPS: Final = (LINKED, NO_REFERENCE)
POPULATION: Final = "qualifying pool commits: not merges, not mechanical"

_POOL_SQL: Final = f"""
    SELECT c.commit_sha, c.topo_order, c.subject, c.message, c.is_merge, c.is_mechanical,
           c.author_name, extract(year FROM c.committed_at AT TIME ZONE 'UTC')::int
    FROM {CHUNKS} d
    JOIN {COMMITS} c ON c.repo_id = d.repo_id AND d.path = 'commit/' || c.commit_sha
    WHERE d.repo_id = %s AND d.kind = 'commit'
    ORDER BY c.topo_order
"""
_PATHS_SQL: Final = f"""
    SELECT r.commit_sha, f.path
    FROM {FILE_REVISIONS} r JOIN {FILES} f ON f.file_id = r.file_id
    WHERE r.repo_id = %s
"""
_TOPO_SQL: Final = f"SELECT topo_order FROM {COMMITS} WHERE repo_id = %s AND commit_sha = %s"


class NothingToCompare(RuntimeError):
    """No commit documents, or no numbering boundary: run `pace pool build` first."""


@dataclass(frozen=True)
class CommitFacts:
    """What the comparison reads of one commit."""

    modules: frozenset[str]
    author: str
    year: int


def module_of(path: str, root: str = MODULE_ROOT) -> str:
    """The first directory under `root`; `(top level)` for a file directly in it."""
    if not path.startswith(root):
        return OTHER
    rest = path[len(root) :]
    return rest.split("/", 1)[0] if "/" in rest else TOP_LEVEL


def module_distribution(touched: Iterable[frozenset[str]]) -> dict[str, float]:
    """Each commit spreads a weight of 1 over the modules it touched; the shares sum to 1."""
    weights: dict[str, float] = {}
    commits = 0
    for modules in touched:
        if not modules:
            continue
        commits += 1
        for module in modules:
            weights[module] = weights.get(module, 0.0) + 1 / len(modules)
    return {module: weights[module] / commits for module in sorted(weights)}


def year_distribution(years: Sequence[int]) -> dict[str, float]:
    counts = Counter(years)
    return {str(year): counts[year] / len(years) for year in sorted(counts)}


def total_variation(p: Mapping[str, float], q: Mapping[str, float]) -> float:
    """Half the L1 distance between two distributions: 0 when identical, 1 when disjoint."""
    return 0.5 * sum(abs(p.get(key, 0.0) - q.get(key, 0.0)) for key in set(p) | set(q))


def author_summary(names: Sequence[str]) -> dict[str, Any]:
    """Distinct authors, and the share of commits made by the ten most frequent."""
    top = Counter(names).most_common(TOP_AUTHORS)
    share = round(sum(n for _, n in top) / len(names), 4) if names else None
    return {"distinct": len(set(names)), "top10_share": share}


def top_authors(names: Sequence[str]) -> set[str]:
    return {name for name, _ in Counter(names).most_common(TOP_AUTHORS)}


def summarise(groups: Mapping[str, Sequence[CommitFacts]]) -> dict[str, Any]:
    """The report's section, from each group's commits. No author name is in it."""
    both = bool(groups[LINKED]) and bool(groups[NO_REFERENCE])
    modules = {g: module_distribution(c.modules for c in rows) for g, rows in groups.items()}
    years = {g: year_distribution([c.year for c in rows]) for g, rows in groups.items()}
    authors = {g: [c.author for c in rows] for g, rows in groups.items()}

    def distance(dists: Mapping[str, Mapping[str, float]]) -> float | None:
        return round(total_variation(dists[LINKED], dists[NO_REFERENCE]), 4) if both else None

    overlap = top_authors(authors[LINKED]) & top_authors(authors[NO_REFERENCE])
    return {
        "population": POPULATION,
        "groups": {g: len(rows) for g, rows in groups.items()},
        "modules": {g: {m: round(s, 4) for m, s in d.items()} for g, d in modules.items()},
        "module_tvd": distance(modules),
        "authors": {
            **{g: author_summary(names) for g, names in authors.items()},
            "top10_overlap": len(overlap),
        },
        "years": {
            g: {str(year): n for year, n in sorted(Counter(c.year for c in rows).items())}
            for g, rows in groups.items()
        },
        "year_tvd": distance(years),
    }


def linkage_bias(
    conn: Any,
    repo_id: int,
    *,
    numbering_starts: str = APACHE_NUMBERING_STARTS,
    module_root: str = MODULE_ROOT,
) -> dict[str, Any]:
    """B2's `linked` against `no_reference` commits, among qualifying pool commits."""
    rows = conn.execute(_POOL_SQL, (repo_id,)).fetchall()
    if not rows:
        raise NothingToCompare(f"repo {repo_id} has no commit documents; run `pace pool build`")
    boundary = conn.execute(_TOPO_SQL, (repo_id, numbering_starts)).fetchone()
    if boundary is None:
        raise NothingToCompare(f"the numbering boundary {numbering_starts} is not in the graph")
    commits = [PoolCommit(row[0], int(row[1]), row[2], row[3]) for row in rows]
    decisions = link_decisions(commits, numbering_starts_at=int(boundary[0]))
    outcome = {d.sha: d.outcome for d in decisions}
    touched: dict[str, set[str]] = {}
    for sha, path in conn.execute(_PATHS_SQL, (repo_id,)).fetchall():
        touched.setdefault(sha, set()).add(module_of(path, module_root))
    groups: dict[str, list[CommitFacts]] = {group: [] for group in GROUPS}
    for sha, _, _, _, is_merge, is_mechanical, author, year in rows:
        if is_merge or is_mechanical or outcome[sha] not in groups:
            continue
        facts = CommitFacts(frozenset(touched.get(sha, set())), author or UNKNOWN_AUTHOR, int(year))
        groups[outcome[sha]].append(facts)
    return summarise(groups)
