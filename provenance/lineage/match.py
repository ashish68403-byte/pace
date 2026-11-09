"""Pass 1 of the lineage engine: one file's history into identity fragments (slice C3a).

ADR-0005 §7 and its Errata of 2026-11-09 (docs/tasks/function-identity/002_lineage_engine.md).
Each revision of one file is compared with the file's last parsed state:

1. the same qualified name is the same identity;
2. among the names that vanished and the names that appeared, an equal name-blind h1 is a rename
   with an unchanged body, then an equal name-blind h3 a rename with local renames; a hash more
   than one leftover shares, on either side, links nothing and is recorded as ambiguous;
3. what is left is a birth or a death.

A revision the chunker refused leaves the state in place. A version is written at birth, and when
a function's token stream (the ordinary h1), its name or its path changes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from provenance.parse.normalize import HashLadder

BIRTH: Final = "birth"
NAME: Final = "name"
H1: Final = "h1"
H3: Final = "h3"
MOVE: Final = "move"


@dataclass(frozen=True)
class Symbol:
    """One function at one revision of one file, hashed over its real source."""

    name: str
    span: tuple[int, int]
    ladder: HashLadder
    blind_h1: str
    blind_h3: str


@dataclass(frozen=True)
class Revision:
    """One file at one commit: its symbols, or None when the chunker refused the text."""

    commit: str
    topo: int
    path: str
    symbols: Mapping[str, Symbol] | None


@dataclass(frozen=True)
class Version:
    """One `symbol_versions` row."""

    commit: str
    topo: int
    path: str
    symbol: Symbol
    stage: str
    is_rename: bool
    is_move: bool
    is_body_change: bool


@dataclass(eq=False)
class Fragment:
    """One identity's run inside one file, from its birth or arrival to its death or the end."""

    file_id: int
    versions: list[Version]
    death: tuple[str, int] | None = None
    ambiguous_birth: bool = False  # pass 2 links neither an ambiguous birth
    ambiguous_death: bool = False  # nor an ambiguous death

    @property
    def last(self) -> Version:
        return self.versions[-1]


@dataclass(frozen=True, eq=False)
class Ambiguity:
    """Leftovers one hash could not pair: nothing is linked, and each pair becomes an edge."""

    commit: str
    topo: int
    stage: str
    key: str
    vanished: tuple[Fragment, ...]
    appeared: tuple[Fragment, ...]


@dataclass
class FilePass:
    fragments: list[Fragment] = field(default_factory=list)
    ambiguities: list[Ambiguity] = field(default_factory=list)
    refused: list[Revision] = field(default_factory=list)


def _next(previous: Version, revision: Revision, symbol: Symbol, stage: str) -> Version:
    return Version(
        commit=revision.commit,
        topo=revision.topo,
        path=revision.path,
        symbol=symbol,
        stage=stage,
        is_rename=symbol.name != previous.symbol.name,
        is_move=revision.path != previous.path,
        is_body_change=symbol.blind_h1 != previous.symbol.blind_h1,
    )


def _pairs(
    vanished: Mapping[str, Fragment], appeared: Mapping[str, Symbol], key: str
) -> tuple[list[tuple[str, str]], list[tuple[str, list[str], list[str]]]]:
    """Leftovers paired by one name-blind hash: the unique pairs, and the hashes several share."""
    old: dict[str, list[str]] = defaultdict(list)
    new: dict[str, list[str]] = defaultdict(list)
    for name, fragment in vanished.items():
        old[getattr(fragment.last.symbol, key)].append(name)
    for name, symbol in appeared.items():
        new[getattr(symbol, key)].append(name)
    unique: list[tuple[str, str]] = []
    shared: list[tuple[str, list[str], list[str]]] = []
    for value in sorted(old.keys() & new.keys()):
        if len(old[value]) == 1 and len(new[value]) == 1:
            unique.append((old[value][0], new[value][0]))
        else:
            shared.append((value, sorted(old[value]), sorted(new[value])))
    return unique, shared


def _born(
    file_id: int,
    revision: Revision,
    symbol: Symbol,
    alive: dict[str, Fragment],
    result: FilePass,
    *,
    ambiguous: bool = False,
) -> Fragment:
    birth = Version(
        revision.commit, revision.topo, revision.path, symbol, BIRTH, False, False, False
    )
    fragment = Fragment(file_id, [birth], ambiguous_birth=ambiguous)
    alive[symbol.name] = fragment
    result.fragments.append(fragment)
    return fragment


def walk_file(file_id: int, revisions: Sequence[Revision]) -> FilePass:
    """Pass 1 over one file's revisions, oldest first."""
    result = FilePass()
    alive: dict[str, Fragment] = {}
    for revision in revisions:
        current = revision.symbols
        if current is None:
            result.refused.append(revision)
            continue
        for name in sorted(alive.keys() & current.keys()):
            fragment, symbol = alive[name], current[name]
            last = fragment.last
            if symbol.ladder.h1 != last.symbol.ladder.h1 or revision.path != last.path:
                fragment.versions.append(_next(last, revision, symbol, NAME))
        vanished = {name: alive[name] for name in sorted(alive.keys() - current.keys())}
        appeared = {name: current[name] for name in sorted(current.keys() - alive.keys())}
        for stage, key in ((H1, "blind_h1"), (H3, "blind_h3")):
            unique, shared = _pairs(vanished, appeared, key)
            for old_name, new_name in unique:
                fragment = vanished.pop(old_name)
                symbol = appeared.pop(new_name)
                fragment.versions.append(_next(fragment.last, revision, symbol, stage))
                del alive[old_name]
                alive[new_name] = fragment
            for value, old_names, new_names in shared:
                gone = tuple(vanished.pop(name) for name in old_names)
                for name, fragment in zip(old_names, gone, strict=True):
                    del alive[name]
                    fragment.death = (revision.commit, revision.topo)
                    fragment.ambiguous_death = True
                born = tuple(
                    _born(file_id, revision, appeared.pop(name), alive, result, ambiguous=True)
                    for name in new_names
                )
                result.ambiguities.append(
                    Ambiguity(revision.commit, revision.topo, stage, value, gone, born)
                )
        for name in sorted(appeared):
            _born(file_id, revision, appeared[name], alive, result)
        for name in sorted(vanished):
            alive.pop(name).death = (revision.commit, revision.topo)
    return result
