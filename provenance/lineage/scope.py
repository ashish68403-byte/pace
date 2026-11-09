"""Following one function's chain across files, for the oracle run (slice C3b).

docs/tasks/function-identity/003_oracle_run.md and ADR-0005's addendum of 2026-11-09: walk the
function's own file and link, then add every Python file touched by a commit where a fragment of
the tracked identity begins, until nothing is added. The cross-file rule links a birth and a
death at one commit, and both lie in files that commit touched, so the identity is the one a
walk over the whole project gives (a test checks this on a fixture).

`link_moves` rewrites the first version of each fragment it links, so every round runs pass 1
afresh over the parsed revisions instead of linking fragments an earlier round rewrote.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from provenance.lineage.link import Lineage, link_moves
from provenance.lineage.match import Revision, walk_file


class ChainRefused(RuntimeError):
    """No identity holds the function alive at the end of its file's history."""


@dataclass(frozen=True)
class Followed:
    """The tracked identity, the files walked to find it, and how many rounds that took."""

    lineage: Lineage
    files: tuple[str, ...]
    rounds: int


def follow_chain(
    path: str,
    name: str,
    revisions_of: Callable[[str], Sequence[Revision]],
    python_touched: Mapping[str, Sequence[str]],
) -> Followed:
    """The identity alive in `path` under `name` at the end, followed into every file it needs."""
    parsed: dict[str, Sequence[Revision]] = {}
    walked = {path}
    rounds = 0
    while True:
        rounds += 1
        ordered = sorted(walked)
        for each in ordered:
            if each not in parsed:
                parsed[each] = revisions_of(each)
        passes = {index: walk_file(index, parsed[each]) for index, each in enumerate(ordered)}
        target = ordered.index(path)
        found = [
            lineage
            for lineage in link_moves(passes).lineages
            if lineage.death is None
            and lineage.fragments[-1].file_id == target
            and lineage.last.symbol.name == name
        ]
        if len(found) != 1:
            raise ChainRefused(f"{len(found)} identities hold {path}::{name} alive at the end")
        lineage = found[0]
        begins = {fragment.versions[0].commit for fragment in lineage.fragments}
        new = {other for commit in begins for other in python_touched.get(commit, ())} - walked
        if not new:
            return Followed(lineage=lineage, files=tuple(ordered), rounds=rounds)
        walked |= new
