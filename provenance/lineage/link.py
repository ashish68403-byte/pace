"""Pass 2 of the lineage engine: moves between files, and lineages out of fragments (slice C3a).

A birth in one file and a death in another, at the same commit, with identical name-blind h3 are
one identity that moved (ADR-0005 §7's one cross-file rule). Each side must have exactly one
candidate; otherwise nothing is linked and the case is recorded. A birth or a death that was
already part of an ambiguous case in pass 1 is left alone.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from provenance.lineage.match import MOVE, Ambiguity, FilePass, Fragment, Version


@dataclass(eq=False)
class Lineage:
    """One identity: its fragments, in order, joined by moves."""

    fragments: list[Fragment]

    @property
    def versions(self) -> list[Version]:
        return [version for fragment in self.fragments for version in fragment.versions]

    @property
    def birth(self) -> Version:
        return self.fragments[0].versions[0]

    @property
    def last(self) -> Version:
        return self.fragments[-1].last

    @property
    def death(self) -> tuple[str, int] | None:
        return self.fragments[-1].death


@dataclass
class Linked:
    lineages: list[Lineage] = field(default_factory=list)
    moves: int = 0
    ambiguities: list[Ambiguity] = field(default_factory=list)


def link_moves(passes: Mapping[int, FilePass]) -> Linked:
    """Pass 2 over every file's fragments, then every chain of fragments as one lineage."""
    births: dict[tuple[str, str], list[Fragment]] = defaultdict(list)
    deaths: dict[tuple[str, str], list[Fragment]] = defaultdict(list)
    for file_id in sorted(passes):
        for fragment in passes[file_id].fragments:
            first = fragment.versions[0]
            if not fragment.ambiguous_birth:
                births[(first.commit, first.symbol.blind_h3)].append(fragment)
            if fragment.death is not None and not fragment.ambiguous_death:
                deaths[(fragment.death[0], fragment.last.symbol.blind_h3)].append(fragment)
    linked = Linked()
    successor: dict[int, Fragment] = {}
    continued: set[int] = set()
    for key in sorted(births.keys() & deaths.keys()):
        born, died = births[key], deaths[key]
        if not any(old.file_id != new.file_id for old in died for new in born):
            continue  # one file's leftovers are pass 1's to pair
        if len(born) == 1 and len(died) == 1:
            dead, newborn = died[0], born[0]
            first, previous = newborn.versions[0], dead.last
            newborn.versions[0] = replace(
                first,
                stage=MOVE,
                is_rename=first.symbol.name != previous.symbol.name,
                is_move=True,
                is_body_change=first.symbol.blind_h1 != previous.symbol.blind_h1,
            )
            successor[id(dead)] = newborn
            continued.add(id(newborn))
            linked.moves += 1
        else:
            commit, value = key
            topo = born[0].versions[0].topo
            linked.ambiguities.append(
                Ambiguity(commit, topo, MOVE, value, tuple(died), tuple(born))
            )
    for file_id in sorted(passes):
        for fragment in passes[file_id].fragments:
            if id(fragment) in continued:
                continue
            chain = [fragment]
            while id(chain[-1]) in successor:
                chain.append(successor[id(chain[-1])])
            linked.lineages.append(Lineage(chain))
    linked.lineages.sort(
        key=lambda lineage: (lineage.birth.topo, lineage.birth.path, lineage.birth.symbol.name)
    )
    return linked
