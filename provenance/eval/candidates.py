"""Slice B3's question candidates: every symbol a commit added or changed, then the rules.

ADR-0003 §3 and its 2026-10-29 addendum say which candidates make an answerable,
unambiguous question (docs/tasks/eval-harness/003_golden_set_generator.md).

The walk reads each in-scope file's revisions in topo order -- the rename-aware history
checkpoint A stored, in which `files.path` is today's path -- and diffs the symbols of
consecutive blobs (`parse.symbols`). Every revision moves the file's state forward, so a
merge or a mechanical commit still updates it; only a non-merge, non-mechanical commit
emits events. The state is `{}` while the file is absent and `None` while it is unknown
(a blob the chunker refuses); a revision with an unknown side emits nothing.

The rules are pure, applied in a fixed order and each counted, so the census can say
what every rule removed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path
from typing import Any, Final

from provenance.graph.tables import COMMITS, FILE_REVISIONS, FILES
from provenance.ingest.git_walk import CatFileBatch
from provenance.parse.symbols import diff_symbols, symbols

__all__ = [
    "ADDED",
    "CHANGED",
    "RULE_ORDER",
    "Event",
    "Rules",
    "SymbolWalk",
    "apply_rules",
    "walk_symbols",
]

ADDED: Final = "added"
CHANGED: Final = "changed"

#: Each template's rules, in the order they apply. A name is a `Rules` method and the
#: census key for the candidates that survived it.
RULE_ORDER: Final[Mapping[str, tuple[str, ...]]] = {
    ADDED: ("at_head", "not_a_move", "added_once"),
    CHANGED: ("at_head", "one_commit_per_month", "unique_name"),
}

_REVISIONS_SQL: Final = f"""
    SELECT r.file_id, f.path, r.commit_sha, r.commit_topo_order, r.change_type::text,
           r.blob_sha, c.is_merge, c.is_mechanical,
           to_char(c.committed_at AT TIME ZONE 'UTC', 'YYYY-MM')
    FROM {FILE_REVISIONS} r
    JOIN {FILES} f ON f.file_id = r.file_id
    JOIN {COMMITS} c ON c.repo_id = r.repo_id AND c.commit_sha = r.commit_sha
    WHERE r.repo_id = %s
    ORDER BY r.file_id, r.commit_topo_order
"""


@dataclass(frozen=True)
class Event:
    """One symbol that one qualifying commit added to, or changed in, one file."""

    template: str  # ADDED or CHANGED
    file_id: int
    path: str  # today's path
    qualified_name: str
    sha: str
    topo_order: int
    month: str  # "YYYY-MM" of committed_at, in UTC (ADR-0003 §3)

    @property
    def short_name(self) -> str:
        return self.qualified_name.rsplit(".", 1)[-1]


@dataclass
class SymbolWalk:
    """The events, what the rules read beside them, and the walk's own counts."""

    events: list[Event] = field(default_factory=list)
    #: commit sha -> (file_id, short name) of every symbol that commit removed from a file
    disappeared: dict[str, set[tuple[int, str]]] = field(default_factory=dict)
    #: file_id -> the qualified names the file holds at HEAD
    head: dict[int, frozenset[str]] = field(default_factory=dict)
    revisions: int = 0
    diffed: int = 0
    skipped_unknown_side: int = 0
    #: blobs the chunker refused, or that the corpus could not supply
    unparseable_blobs: int = 0


def walk_symbols(conn: Any, repo_id: int, corpus: Path) -> SymbolWalk:
    """Diff every in-scope revision's symbols against its file's previous state."""
    rows = conn.execute(_REVISIONS_SQL, (repo_id,)).fetchall()
    walk = SymbolWalk()
    with CatFileBatch(corpus) as cat:
        for file_id, revisions in groupby(rows, key=lambda row: int(row[0])):
            state: dict[str, str] | None = {}
            for _, path, sha, topo, change, blob, is_merge, is_mechanical, month in revisions:
                walk.revisions += 1
                after = _symbols_after(cat, change, blob, path)
                if after is None:
                    walk.unparseable_blobs += 1
                qualifying = not is_merge and not is_mechanical
                if state is None or after is None:
                    walk.skipped_unknown_side += int(qualifying)
                    state = after
                    continue
                for name in state.keys() - after.keys():
                    walk.disappeared.setdefault(sha, set()).add((file_id, _short(name)))
                if qualifying:
                    walk.diffed += 1
                    diff = diff_symbols(state, after)
                    for template, names in ((ADDED, diff.added), (CHANGED, diff.changed)):
                        for name in names:
                            walk.events.append(
                                Event(template, file_id, path, name, sha, int(topo), month)
                            )
                state = after
            walk.head[file_id] = frozenset(state or {})
    return walk


def _short(qualified_name: str) -> str:
    return qualified_name.rsplit(".", 1)[-1]


def _symbols_after(cat: CatFileBatch, change: str, blob: str, path: str) -> dict[str, str] | None:
    """The file's symbols after one revision: `{}` once deleted, `None` when unreadable."""
    if change == "deleted":
        return {}
    data = cat.read(blob)
    if data is None:
        return None
    return symbols(data.decode("utf-8", "replace"), path)


@dataclass(frozen=True)
class Rules:
    """ADR-0003 §3's rules and the 2026-10-29 addendum's, over one walk."""

    head: Mapping[int, frozenset[str]]
    disappeared: Mapping[str, set[tuple[int, str]]]
    times_added: Counter[tuple[int, str]]
    changes_in_month: Counter[tuple[int, str, str]]
    files_with_name: Counter[str]

    @classmethod
    def of(cls, walk: SymbolWalk) -> Rules:
        added = [e for e in walk.events if e.template == ADDED]
        changed = [e for e in walk.events if e.template == CHANGED]
        return cls(
            head=walk.head,
            disappeared=walk.disappeared,
            times_added=Counter((e.file_id, e.qualified_name) for e in added),
            changes_in_month=Counter((e.file_id, e.qualified_name, e.month) for e in changed),
            files_with_name=Counter(name for names in walk.head.values() for name in names),
        )

    def at_head(self, event: Event) -> bool:
        """The name still exists at HEAD in the same rename-aware file (§3)."""
        return event.qualified_name in self.head.get(event.file_id, frozenset())

    def not_a_move(self, event: Event) -> bool:
        """No symbol with this short name left another file in the same commit (§3)."""
        return not any(
            file_id != event.file_id and short == event.short_name
            for file_id, short in self.disappeared.get(event.sha, set())
        )

    def added_once(self, event: Event) -> bool:
        """The name was added exactly once in this file's history (addendum)."""
        return self.times_added[(event.file_id, event.qualified_name)] == 1

    def one_commit_per_month(self, event: Event) -> bool:
        """Exactly one qualifying commit changed it, in this file, that month (§3)."""
        return self.changes_in_month[(event.file_id, event.qualified_name, event.month)] == 1

    def unique_name(self, event: Event) -> bool:
        """The name exists in exactly one file at HEAD; the question names no file (addendum)."""
        return self.files_with_name[event.qualified_name] == 1


def apply_rules(walk: SymbolWalk) -> tuple[list[Event], dict[str, dict[str, int]]]:
    """The events every rule keeps, and how many survived each rule, per template."""
    rules = Rules.of(walk)
    survivors: list[Event] = []
    counts: dict[str, dict[str, int]] = {}
    for template, order in RULE_ORDER.items():
        kept = [e for e in walk.events if e.template == template]
        counts[template] = {"raw": len(kept)}
        for name in order:
            keep = getattr(rules, name)
            kept = [e for e in kept if keep(e)]
            counts[template][name] = len(kept)
        survivors.extend(kept)
    return survivors, counts
